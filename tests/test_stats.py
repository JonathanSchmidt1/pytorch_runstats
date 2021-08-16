from typing import Optional
import functools

import pytest

import random

import torch
from torch_scatter import scatter

from torch_runstats import RunningStats, Reduction


@pytest.fixture(scope="module")
def allclose(float_tolerance):
    return functools.partial(torch.allclose, atol=float_tolerance)


class StatsTruth(RunningStats):
    """Inefficient ground truth for RunningStats by directly storing all data"""

    def accumulate_batch(
        self, batch: torch.Tensor, accumulate_by: Optional[torch.Tensor] = None
    ) -> torch.Tensor:

        if accumulate_by is None:
            accumulate_by = torch.zeros(len(batch), dtype=torch.long)
        if hasattr(self, "_state"):
            self._state = torch.cat((self._state, batch), dim=0)
            self._acc = torch.cat((self._acc, accumulate_by), dim=0)
        else:
            self._state = batch.clone()
            self._acc = accumulate_by.clone()
            self._n_bins = 1

        if self._acc.max() + 1 > self._n_bins:
            self._n_bins = int(self._acc.max() + 1)

        average, _, _ = self.batch_result(batch, accumulate_by)
        return average

    def reset(self, reset_n_bins: bool = False) -> None:
        if hasattr(self, "_state"):
            delattr(self, "_state")
            delattr(self, "_acc")
        if reset_n_bins:
            self._n_bins = 1

    def current_result(self):
        if not hasattr(self, "_state"):
            return torch.zeros(self._dim)
        average, _, _ = self.batch_result(self._state, self._acc)
        
        if len(average) < self._n_bins:
            N_to_add = self._n_bins - len(average)
            average = torch.cat((average, torch.zeros((N_to_add,)+average.shape[1:])))

        return average


@pytest.mark.parametrize(
    "dim,reduce_dims,nan_attrs",
    [
        (1, tuple(), False),
        (1, (0,), True),
        (3, tuple(), False),
        (3, (0,), True),
        ((2, 3), tuple(), False),
        (torch.Size((1, 2, 1)), tuple(), False),
        (torch.Size((1, 2, 1)), (1,), False),
        (torch.Size((3, 2, 4)), (0, 2), False),
        (torch.Size((3, 2, 4)), (0, 1, 2), True),
    ],
)
@pytest.mark.parametrize("reduction", [Reduction.MEAN, Reduction.RMS])
@pytest.mark.parametrize("do_accumulate_by", [True, False])
def test_runstats(dim, reduce_dims, nan_attrs, reduction, do_accumulate_by, allclose):

    n_batchs = (random.randint(1, 4), random.randint(1, 4))
    truth_obj = StatsTruth(dim=dim, reduction=reduction, reduce_dims=reduce_dims)
    runstats = RunningStats(dim=dim, reduction=reduction, reduce_dims=reduce_dims)

    for n_batch in n_batchs:
        for _ in range(n_batch):
            batch = torch.randn((random.randint(1, 10),) + runstats.dim)
            if nan_attrs:
                batch.view(-1)[0] = float("NaN")

            if do_accumulate_by and random.choice((True, False)):
                accumulate_by = torch.randint(
                    0, random.randint(1, 5), size=(batch.shape[0],)
                )
            else:
                accumulate_by = None

            truth = truth_obj.accumulate_batch(batch, accumulate_by=accumulate_by)
            res = runstats.accumulate_batch(batch, accumulate_by=accumulate_by)
            assert allclose(truth, res)
        truth = truth_obj.current_result()
        res = runstats.current_result()
        assert allclose(truth, res)
        truth_obj.reset(reset_n_bins=True)
        runstats.reset(reset_n_bins=True)


@pytest.mark.parametrize("reduction", [Reduction.MEAN, Reduction.RMS])
def test_zeros(reduction, allclose):
    dim = (4,)
    runstats = RunningStats(dim=dim, reduction=reduction)
    assert allclose(runstats.current_result(), torch.zeros(dim))
    runstats.accumulate_batch(torch.randn((3,) + dim))
    runstats.reset()
    assert allclose(runstats.current_result(), torch.zeros(dim))


def test_raises():
    runstats = RunningStats(dim=4, reduction=Reduction.MEAN)
    with pytest.raises(ValueError):
        runstats.accumulate_batch(torch.zeros(10, 2))


@pytest.mark.parametrize(
    "dim,reduce_dims",
    [
        (1, tuple()),
        (3, tuple()),
        ((2, 3), tuple()),
        (torch.Size((1, 2, 1)), tuple()),
        (torch.Size((1, 2, 1)), (1,)),
        (torch.Size((3, 2, 4)), (0, 2)),
    ],
)
@pytest.mark.parametrize("reduction", [Reduction.MEAN, Reduction.RMS])
@pytest.mark.parametrize("do_accumulate_by", [True, False])
def test_one_acc(dim, reduce_dims, reduction, do_accumulate_by, allclose):
    runstats = RunningStats(dim=dim, reduction=reduction, reduce_dims=reduce_dims)
    reduce_in_dims = tuple(i + 1 for i in reduce_dims)
    batch = torch.randn((random.randint(3, 10),) + runstats.dim)
    if do_accumulate_by:
        accumulate_by = torch.randint(0, random.randint(1, 5), size=(batch.shape[0],))
        res = runstats.accumulate_batch(batch, accumulate_by=accumulate_by)

        if reduction == Reduction.RMS:
            batch = batch.square()

        outs = []
        for i in range(max(accumulate_by) + 1):
            tmp = batch[accumulate_by == i].mean(dim=(0,) + reduce_in_dims)
            torch.nan_to_num_(tmp, nan=0.0)
            outs.append(tmp)

        truth = torch.stack(outs, dim=0)
        assert truth.shape[1:] == tuple(
            d for i, d in enumerate(runstats.dim) if i not in reduce_dims
        )

        if reduction == Reduction.RMS:
            truth.sqrt_()
    else:
        res = runstats.accumulate_batch(batch)
        if reduction == Reduction.MEAN:
            truth = batch.mean(dim=(0,) + reduce_in_dims)
        elif reduction == Reduction.RMS:
            truth = batch.square().mean(dim=(0,) + reduce_in_dims).sqrt()

    assert allclose(truth, res)
    assert allclose(truth, runstats.current_result())
