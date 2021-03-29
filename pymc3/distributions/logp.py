#   Copyright 2020 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from functools import singledispatch
from typing import Optional

import aesara.tensor as at
import numpy as np

from aesara import config
from aesara.graph.basic import graph_inputs, io_toposort
from aesara.graph.op import Op, compute_test_value
from aesara.tensor.random.op import RandomVariable
from aesara.tensor.subtensor import AdvancedSubtensor, AdvancedSubtensor1, Subtensor
from aesara.tensor.var import TensorVariable

from pymc3.aesaraf import extract_rv_and_value_vars, floatX, rvs_to_value_vars


@singledispatch
def logp_transform(op: Op):
    return None


def _get_scaling(total_size, shape, ndim):
    """
    Gets scaling constant for logp

    Parameters
    ----------
    total_size: int or list[int]
    shape: shape
        shape to scale
    ndim: int
        ndim hint

    Returns
    -------
    scalar
    """
    if total_size is None:
        coef = floatX(1)
    elif isinstance(total_size, int):
        if ndim >= 1:
            denom = shape[0]
        else:
            denom = 1
        coef = floatX(total_size) / floatX(denom)
    elif isinstance(total_size, (list, tuple)):
        if not all(isinstance(i, int) for i in total_size if (i is not Ellipsis and i is not None)):
            raise TypeError(
                "Unrecognized `total_size` type, expected "
                "int or list of ints, got %r" % total_size
            )
        if Ellipsis in total_size:
            sep = total_size.index(Ellipsis)
            begin = total_size[:sep]
            end = total_size[sep + 1 :]
            if Ellipsis in end:
                raise ValueError(
                    "Double Ellipsis in `total_size` is restricted, got %r" % total_size
                )
        else:
            begin = total_size
            end = []
        if (len(begin) + len(end)) > ndim:
            raise ValueError(
                "Length of `total_size` is too big, "
                "number of scalings is bigger that ndim, got %r" % total_size
            )
        elif (len(begin) + len(end)) == 0:
            return floatX(1)
        if len(end) > 0:
            shp_end = shape[-len(end) :]
        else:
            shp_end = np.asarray([])
        shp_begin = shape[: len(begin)]
        begin_coef = [floatX(t) / shp_begin[i] for i, t in enumerate(begin) if t is not None]
        end_coef = [floatX(t) / shp_end[i] for i, t in enumerate(end) if t is not None]
        coefs = begin_coef + end_coef
        coef = at.prod(coefs)
    else:
        raise TypeError(
            "Unrecognized `total_size` type, expected int or list of ints, got %r" % total_size
        )
    return at.as_tensor(floatX(coef))


def logpt(
    rv_var: TensorVariable,
    rv_value: Optional[TensorVariable] = None,
    *,
    jacobian: bool = True,
    scaling: bool = True,
    transformed: bool = True,
    cdf: bool = False,
    sum: bool = False,
    **kwargs,
) -> TensorVariable:
    """Create a measure-space (i.e. log-likelihood) graph for a random variable at a given point.

    The input `rv_var` determines which log-likelihood graph is used and
    `rv_value` is that graph's input parameter.  For example, if `rv_var` is
    the output of a `NormalRV` `Op`, then the output is
    ``normal_log_pdf(rv_value)``.

    Parameters
    ==========
    rv_var
        The `RandomVariable` output that determines the log-likelihood graph.
    rv_value
        The variable that represents the value of `rv_var` in its
        log-likelihood.  If no value is provided, `rv_var.tag.value_var` will
        be checked and, when available, used.
    jacobian
        Whether or not to include the Jacobian term.
    scaling
        A scaling term to apply to the generated log-likelihood graph.
    transformed
        Apply transforms.
    cdf
        Return the log cumulative distribution.
    sum
        Sum the log-likelihood.

    """

    rv_var, rv_value_var = extract_rv_and_value_vars(rv_var)

    if rv_value is None:

        if rv_value_var is None:
            raise ValueError(f"No value variable specified or associated with {rv_var}")

        rv_value = rv_value_var
    else:
        rv_value = at.as_tensor(rv_value)

        # Make sure that the value is compatible with the random variable
        rv_value = rv_var.type.filter_variable(rv_value.astype(rv_var.dtype))

        if rv_value_var is None:
            rv_value_var = rv_value

    rv_node = rv_var.owner

    if not rv_node:
        return at.zeros_like(rv_var)

    if not isinstance(rv_node.op, RandomVariable):
        return _logp(rv_node.op, rv_value, rv_node.inputs)

    rng, size, dtype, *dist_params = rv_node.inputs

    # Here, we plug the actual random variable into the log-likelihood graph,
    # because we want a log-likelihood graph that only contains
    # random variables.  This is important, because a random variable's
    # parameters can contain random variables themselves.
    # Ultimately, with a graph containing only random variables and
    # "deterministics", we can simply replace all the random variables with
    # their value variables and be done.
    if not cdf:
        logp_var = _logp(rv_node.op, rv_var, *dist_params, **kwargs)
    else:
        logp_var = _logcdf(rv_node.op, rv_var, *dist_params, **kwargs)

    transform = getattr(rv_value_var.tag, "transform", None) if rv_value_var else None

    if transform and transformed and not cdf and jacobian:
        transformed_jacobian = transform.jacobian_det(rv_var, rv_value)
        if transformed_jacobian:
            if logp_var.ndim > transformed_jacobian.ndim:
                logp_var = logp_var.sum(axis=-1)
            logp_var += transformed_jacobian

    # Replace random variables with their value variables
    (logp_var,), _ = rvs_to_value_vars(
        (logp_var,),
        apply_transforms=transformed and not cdf,
        initial_replacements={rv_var: rv_value, rv_value_var: rv_value},
    )

    if sum:
        logp_var = at.sum(logp_var)

    if scaling:
        logp_var *= _get_scaling(
            getattr(rv_var.tag, "total_size", None), rv_value.shape, rv_value.ndim
        )

    # Recompute test values for the changes introduced by the replacements
    # above.
    if config.compute_test_value != "off":
        for node in io_toposort(graph_inputs((logp_var,)), (logp_var,)):
            compute_test_value(node)

    if rv_var.name is not None:
        logp_var.name = "__logp_%s" % rv_var.name

    return logp_var


@singledispatch
def _logp(op: Op, value: TensorVariable, *dist_params, **kwargs):
    """Create a log-likelihood graph.

    This function dispatches on the type of `op`, which should be a subclass
    of `RandomVariable`.  If you want to implement new log-likelihood graphs
    for a `RandomVariable`, register a new function on this dispatcher.

    """
    return at.zeros_like(value)


@_logp.register(Subtensor)
@_logp.register(AdvancedSubtensor)
@_logp.register(AdvancedSubtensor1)
def subtensor_logp(op, value, *inputs, **kwargs):

    # TODO: Compute the log-likelihood for a subtensor/index operation.
    raise NotImplementedError()

    # "Flatten" and sum an array of indexed RVs' log-likelihoods
    # rv_var, missing_values =
    #
    # missing_values = missing_values.data
    # logp_var = at.sum(
    #     [
    #         logpt(
    #             rv_var,
    #         )
    #         for idx, missing in zip(
    #             np.ndindex(missing_values.shape), missing_values.flatten()
    #         )
    #         if missing
    #     ]
    # )
    # return logp_var


def logcdf(*args, **kwargs):
    """Create a log-CDF graph."""
    return logpt(*args, cdf=True, **kwargs)


@singledispatch
def _logcdf(op, value, *args, **kwargs):
    """Create a log-CDF graph.

    This function dispatches on the type of `op`, which should be a subclass
    of `RandomVariable`.  If you want to implement new log-CDF graphs
    for a `RandomVariable`, register a new function on this dispatcher.

    """
    raise NotImplementedError()


def logpt_sum(*args, **kwargs):
    """Return the sum of the logp values for the given observations.

    Subclasses can use this to improve the speed of logp evaluations
    if only the sum of the logp values is needed.
    """
    return logpt(*args, sum=True, **kwargs)