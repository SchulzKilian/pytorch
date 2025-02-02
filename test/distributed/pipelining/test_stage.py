# Copyright (c) Meta Platforms, Inc. and affiliates
# Owner(s): ["oncall: distributed"]
import os
import sys
import tempfile

from model_registry import ExampleCode, ModelWithKwargs, MultiMLP

import torch
import torch.distributed as dist
from torch.distributed.pipelining import (
    pipeline,
    PipelineStage,
    ScheduleGPipe,
    TracerPipelineStage,
)
from torch.distributed.pipelining._utils import PipeliningShapeError
from torch.testing._internal.common_cuda import TEST_MULTIGPU
from torch.testing._internal.common_distributed import (
    MultiProcContinousTest,
    requires_nccl,
)
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
    skip_but_pass_in_sandcastle_if,
)
from torch.utils._pytree import tree_map_only

d_hid = 512
batch_size = 256
chunks = 4

torch.manual_seed(0)


def get_dtype_change_hook(new_dtype):
    """A simple hook for simulating mixed precision"""

    def dtype_change_hook(module, input, output):
        def f(x):
            return x.to(new_dtype)

        return tree_map_only(torch.Tensor, f, output)

    return dtype_change_hook


def get_flatten_hook():
    """A simple hook for simulating wrong model output shape"""

    def flatten_hook(module, input, output):
        def f(x):
            return x.flatten()

        return tree_map_only(torch.Tensor, f, output)

    return flatten_hook


class StageTest(MultiProcContinousTest):
    @classmethod
    def backend_str(cls) -> str:
        # Testing with NCCL backend
        return "nccl"

    @classmethod
    def setUpClass(cls):
        """
        Class-scope test fixture. Run once for entire test class, before any test starts.
        Set up the device.
        """
        super().setUpClass()
        dev_id = cls.rank % torch.cuda.device_count()
        cls.device = torch.device(f"cuda:{dev_id}")

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ModelClass", [ExampleCode, MultiMLP])
    def test_tracer(self, ModelClass):
        mod = ModelClass(d_hid)
        mod.to(self.device)

        x = torch.randn(batch_size, d_hid, device=self.device)

        split_spec = mod.split_spec if hasattr(mod, "split_spec") else None
        pipe = pipeline(
            mod,
            chunks,
            example_args=(x,),
            split_spec=split_spec,
        )

        stage = TracerPipelineStage(
            pipe,
            self.rank,
            device=self.device,
        )

        # Attach to a schedule
        schedule = ScheduleGPipe(stage, chunks)

        # Run
        def _run_step(x):
            if self.rank == 0:
                return schedule.step(x)
            else:
                return schedule.step()

        out = _run_step(x)
        # Last rank checks result
        if self.rank == self.world_size - 1:
            ref_out = mod(x)
            torch.testing.assert_close(out, ref_out, atol=1e-3, rtol=5e-2)

        # Test qualname mapping
        submod_keys = stage.submod.state_dict().keys()
        # Confirm keys are consistent with original model
        old_keys = mod.state_dict().keys()
        assert all(k in old_keys for k in submod_keys)

        if self.rank == 0:
            # intended to run this code on all ranks, but the problem is if rank0 throws,
            # it won't perform the send that unblocks rank 1.

            # TODO(whc) can't test this until fixing args/kwargs issue
            # with self.assertRaisesRegex(PipeliningShapeError, "shape mismatch"):
            #     _run_step(torch.randn(batch_size + 1, d_hid, device=self.device))

            with self.assertRaisesRegex(PipeliningShapeError, "dtype mismatch"):
                _run_step(x.to(torch.int32))

            # output of stage's mlp layer will be flattened by this hook, the stage should err
            handle = stage.submod.register_forward_hook(get_flatten_hook())
            with self.assertRaisesRegex(PipeliningShapeError, "shape mismatch"):
                _run_step(x)
            handle.remove()

            stage.submod.register_forward_hook(get_dtype_change_hook(torch.bfloat16))
            with self.assertRaisesRegex(PipeliningShapeError, "dtype mismatch"):
                _run_step(x)

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ModelClass", [ModelWithKwargs])
    def test_tracer_kwargs(self, ModelClass):
        mod = ModelClass(d_hid)
        mod.to(self.device)

        x = torch.randn(batch_size, d_hid, device=self.device)
        y = torch.randn(batch_size, d_hid, device=self.device)

        pipe = pipeline(
            mod,
            chunks,
            example_args=(x,),
            example_kwargs={"y": y},
        )

        stage = TracerPipelineStage(
            pipe,
            self.rank,
            device=self.device,
        )

        # Attach to a schedule
        schedule = ScheduleGPipe(stage, chunks)

        # Run
        def _run_step(x):
            if self.rank == 0:
                return schedule.step(x, y=y)
            else:
                return schedule.step()

        # Last rank checks result
        out = _run_step(x)
        if self.rank == self.world_size - 1:
            ref_out = mod(x, y=y)
            torch.testing.assert_close(out, ref_out, atol=1e-3, rtol=5e-2)

        # Test qualname mapping
        submod_keys = stage.submod.state_dict().keys()
        # Confirm keys are consistent with original model
        old_keys = mod.state_dict().keys()
        assert all(k in old_keys for k in submod_keys)

        if self.rank == 0:
            with self.assertRaisesRegex(PipeliningShapeError, "shape mismatch"):
                _run_step(torch.randn(batch_size + 1, d_hid, device=self.device))

            with self.assertRaisesRegex(PipeliningShapeError, "dtype mismatch"):
                _run_step(x.to(torch.int32))

            # output of stage's mlp layer will be flattened by this hook, the stage should err
            handle = stage.submod.register_forward_hook(get_flatten_hook())
            with self.assertRaisesRegex(PipeliningShapeError, "shape mismatch"):
                _run_step(x)
            handle.remove()

            stage.submod.register_forward_hook(get_dtype_change_hook(torch.bfloat16))
            with self.assertRaisesRegex(PipeliningShapeError, "dtype mismatch"):
                _run_step(x)

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    def test_manual(self):
        full_mod = MultiMLP(d_hid, n_layers=self.world_size)
        full_mod.to(self.device)
        stage_mod = full_mod.get_submodule(f"layers.{self.rank}")

        x = torch.randn(batch_size, d_hid, device=self.device)

        stage = PipelineStage(
            stage_mod,
            self.rank,
            self.world_size,
            self.device,
            chunks,
            input_args=x.chunk(chunks)[0],
        )

        # Attach to a schedule
        schedule = ScheduleGPipe(stage, chunks)

        # Run
        def _run_step(x):
            if self.rank == 0:
                return schedule.step(x)
            else:
                return schedule.step()

        out = _run_step(x)
        # Last rank checks result
        if self.rank == self.world_size - 1:
            ref_out = full_mod(x)
            torch.testing.assert_close(out, ref_out)

        if self.rank == 0:
            with self.assertRaisesRegex(PipeliningShapeError, "shape mismatch"):
                _run_step(torch.randn(batch_size + 1, d_hid, device=self.device))

            with self.assertRaisesRegex(PipeliningShapeError, "dtype mismatch"):
                _run_step(x.to(torch.int32))

            # output of stage's mlp layer will be flattened by this hook, the stage should err
            handle = stage_mod.register_forward_hook(get_flatten_hook())
            with self.assertRaisesRegex(PipeliningShapeError, "shape mismatch"):
                _run_step(x)
            handle.remove()

            stage_mod.register_forward_hook(get_dtype_change_hook(torch.bfloat16))
            with self.assertRaisesRegex(PipeliningShapeError, "dtype mismatch"):
                _run_step(x)


instantiate_parametrized_tests(StageTest)

if __name__ == "__main__":
    # Check if GPU and NCCL are available
    if not (
        dist.is_available()
        and dist.is_nccl_available()
        and torch.cuda.device_count() > 1
    ):
        print(
            "c10d NCCL not available or not enough GPUs, skipping tests",
            file=sys.stderr,
        )
        sys.exit(0)

    rank = int(os.getenv("RANK", -1))
    world_size = int(os.getenv("WORLD_SIZE", 2))

    if rank != -1:
        # Launched with torchrun or other multi-proc launchers. Directly run the test.
        StageTest.run_rank(rank, world_size)
    else:
        # Launched as a single process. Spawn subprocess to run the tests.
        # Also need a rendezvous file for `init_process_group` purpose.
        rdvz_file = tempfile.NamedTemporaryFile(delete=False).name
        torch.multiprocessing.spawn(
            StageTest.run_rank,
            nprocs=world_size,
            args=(world_size, rdvz_file),
        )
