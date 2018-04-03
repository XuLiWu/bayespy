################################################################################
# Copyright (C) 2017 Jaakko Luttinen
#
# This file is licensed under the MIT License.
################################################################################


import numpy as np
import junctiontree as jt
import attr


from .node import Node
from .stochastic import Distribution

from .dirichlet import DirichletMoments

from bayespy.utils import misc


@attr.s(frozen=True, slots=True)
class ConditionalProbabilityTable():


    variable = attr.ib()
    table = attr.ib(converter=lambda x: Node._ensure_moments(x, DirichletMoments))
    given = attr.ib(converter=tuple, default=())
    plates = attr.ib(converter=tuple, default=())


@attr.s(frozen=True, slots=True)
class Marginal():


    name = attr.ib()
    variables = attr.ib(converter=tuple)
    plates = attr.ib(converter=tuple, default=())


@attr.s(frozen=True, slots=True)
class Factor():


    name = attr.ib()
    potential = attr.ib()
    variables = attr.ib(converter=tuple)
    plates = attr.ib(converter=tuple, default=())


def find_index(xs, x):
    return xs.index(x) - len(xs)


def take(x, ind, axis):
    """
    Take elements along the last axis but apply the shape to the leading axes.

    That is, for ind with ndim=2:

    y[i,j] = x_ij[i,j,...,ind[i,j]] for i=1,...,M and j=1,...,N

    """
    x = np.asanyarray(x)
    ndim = np.ndim(x)
    if axis >= 0:
        axis = axis - ndim
    if axis >= 0 or axis < -ndim:
        raise ValueError("Axis out of bounds")
    shape = np.shape(x)
    plates = shape[:np.ndim(ind)]
    n_plates = len(plates)
    ind_plates = np.ix_(*[range(plate) for plate in plates])
    inds = list(ind_plates) + [...] + [np.asarray(ind)] + (abs(axis) - 1) * [slice(None)]
    return np.expand_dims(x[inds], axis)


def onehot(index, size, axis=-1, extradims=0):
    if extradims < 0:
        raise ValueError("extradims must be non-negative")
    index = np.reshape(index, np.shape(index) + (extradims + 1) * (1,))
    return 1.0 * np.moveaxis(
         np.arange(size) == index,
        -1,
        axis
    )


def map_to_plates(x, src, dst):
    dst_keys = list(range(len(dst)))
    src_keys = [dst.index(i) for i in src]
    return np.einsum(
        np.ones((1,) * len(dst_keys), dtype=np.int),
        dst_keys,
        x,
        src_keys,
        dst_keys
    )


def map_to_shape(sizes, keys):
    return tuple(sizes[key] for key in keys)


class CategoricalGraph():
    """DAG for categorical variables with exact inference

    The exact inference is uses the Junction tree algorithm.

    A simple example showing basically all available features:

    >>> dag = CategoricalGraph(
    ...     {
    ...         "x": {
    ...             "table": [0.4, 0.6],
    ...         },
    ...         "y": {
    ...             "given": ["x"],
    ...             "plates": ["trials"],
    ...             "table": [ [0.1, 0.3, 0.6], [0.8, 0.1, 0.1] ],
    ...         },
    ...     },
    ...     plates={
    ...         "trials": 10,
    ...     },
    ...     marginals={
    ...         "marg_y": {
    ...             "variables": ["y"],
    ...             "plates": ["trials"],
    ...         },
    ...     },
    ... )
    >>> dag.update()
    >>> print(dag["x"].p)
    >>> print(dag["y"].p)
    >>> print(dag["marg_y"].p)
    >>> dag.observe({"y": [1, 2, 0, 0, 2, 2, 1, 2, 1, 0]})
    >>> dag.update()
    >>> print(dag["x"].p)
    >>> print(dag["y"].p)
    >>> print(dag["marg_y"].p)

    """

    # TODO:
    #
    # - random
    # - message to parent
    # - explicit extra marginals: CategoricalGraph({...}, plates={...}, marginals={...})
    # - message to children
    # - multi-dimensional categorical moments, support in mixture
    # - compare performance to categoricalchain
    # - implement categorical tree as an example #15 and #20
    # - implement stochastic block model #51
    # - example graph #23


    def __init__(self, dag, plates={}, marginals={}):

        # Convert to CPTs
        dag = {
            name: ConditionalProbabilityTable(variable=name, **config)
            for (name, config) in dag.items()
        }

        # Convert to Marginals
        marginals = {
            name: Marginal(name=name, **config)
            for (name, config) in marginals.items()
        }

        # Validate plates (children must have those plates that the parents have)

        # Validate shapes of the CPTs

        # Validate that plate keys and variable keys are unique


        # Fix the order of CPTs. Each CPT corresponds to a factor.
        self._cpts = list(dag.values())
        self._dag = dag

        # Add a factor for each CPT and requested marginal
        def get_potential(node):
            return lambda: np.exp(node.table.get_moments()[0])
        self._factors = [
            Factor(
                name=cpt.variable,
                variables=cpt.given + (cpt.variable,),
                plates=cpt.plates,
                potential=get_potential(cpt)
                #potential=lambda: np.exp(cpt.table.get_moments()[0]),
            )
            for cpt in self._cpts
        ] + [
            Factor(
                name=marginal.name,
                variables=marginal.variables,
                plates=marginal.plates,
                potential=lambda: 1.0,
            )
            for marginal in marginals.values()
        ]

        # Mapping: factor -> keys (i.e., variables and plates) in the factor
        #
        # This is required by Junctiontree package.
        self._keys_in_factor = [
            factor.plates + factor.variables
            for factor in self._factors
        ]

        # Mapping: variable -> factors
        #
        # FIXME: Reverse mapping, should be done in junctiontree package? For
        # each variable, find the list of factors in which the variable is
        # included.
        self._factors_with_variable = {
            variable: [
                (
                    # Factor ID
                    index,
                    # The axis of this variable in the CPT array (as a negative axis)
                    find_index(factor.variables, variable)
                )
                for (index, factor) in enumerate(self._factors)
                if variable in factor.variables
            ]
            for variable in dag.keys()
        }

        # Number of states for each variable (CPTs are assumed Dirichlet moments here)
        variable_sizes = {
            cpt.variable: cpt.table.dims[0][0]
            for cpt in self._cpts
        }

        # Sizes of all axes (variables and plates), that is, just combine the
        # two size dicts
        all_sizes = list(variable_sizes.items()) + list(plates.items())
        self._original_sizes = {
            key: size for (key, size) in all_sizes
        }
        self._factor_shapes = [
            map_to_shape(self._original_sizes, factor.plates + factor.variables)
            for factor in self._factors
        ]

        # State
        self._junctiontree = None
        self._sizes = self._original_sizes
        self._slice_potentials = lambda xs: xs
        self._unslice_potentials = lambda xs: xs
        self.u = {
            cpt.variable: np.nan for cpt in self._cpts
        }

        return


    def lower_bound_contribution(self):
        raise NotImplementedError()


    def get_moments(self):
        return self.u


    def observe(self, y):
        """Give dictionary like {"rain": 1, "sunny": 0}.

        NOTE: Previously set observed states are reset.
        """

        # Create a function to slice the potential arrays. This is used for
        # observing a variable: only use that state which was observed.
        def slice_potentials(xs):
            xs = xs.copy()
            # Loop observations
            for (variable, ind) in y.items():
                ind = np.asarray(ind, dtype=np.int)
                # Loop all factors that contain the observed variable
                for (factor, axis) in self._factors_with_variable[variable]:
                    xs[factor] = take(
                        xs[factor],
                        map_to_plates(
                            ind,
                            src=self._dag[variable].plates,
                            dst=self._factors[factor].plates
                        ),
                        axis
                    )
            return xs


        def unslice_potentials(xs):
            xs = xs.copy()
            for (variable, ind) in y.items():
                for (factor, axis) in self._factors_with_variable[variable]:
                    plates = self._factors[factor].plates
                    e = onehot(
                        index=map_to_plates(
                            ind,
                            src=self._dag[variable].plates,
                            dst=plates
                        ),
                        size=self._original_sizes[variable],
                        extradims=np.ndim(xs[factor]) - len(plates) - 1,
                        axis=axis,
                    )
                    xs[factor] = e * xs[factor]
            return xs

        # TODO: Validate observation array shapes

        # Modify sizes
        self._sizes = self._original_sizes.copy()
        self._sizes.update({key: 1 for key in y.keys()})

        # Junction tree needs to be rebuilt
        self._junctiontree = None
        self._slice_potentials = slice_potentials
        self._unslice_potentials = unslice_potentials
        self.u = {
            cpt.variable: np.nan for cpt in self._cpts
        }

        return


    def update(self):
        # TODO: Fetch CPTs from Dirichlet parents and make use of potentials
        # from children.

        # FIXME: Convert to lists.. Fix this in junctiontree
        factors = [list(f) for f in self._keys_in_factor]

        if self._junctiontree is None:
            self._junctiontree = jt.create_junction_tree(
                #self._keys_in_factor,
                factors,
                self._sizes
            )

        # Get the numerical probability tables from the Dirichlet nodes
        #
        # FIXME: Convert <log p> to exp( <log p> ). Perhaps junctiontree
        # package could support logarithms of the probabilities? Also, note
        # that these don't sum to one, they are non-normalized probabilities.
        potentials = [
            np.broadcast_to(factor.potential(), shape)
            for (shape, factor) in zip(self._factor_shapes, self._factors)
        ]

        xs = self._slice_potentials(potentials)
        # Convert to lists..
        u = self._junctiontree.propagate(list(xs))

        def _normalize(p, n_plates=0):
            return p / np.sum(p, axis=tuple(range(n_plates, np.ndim(p))), keepdims=True)

        self.u = {
            factor.name: _normalize(ui, n_plates=len(factor.plates))
            for (factor, ui) in zip(self._factors, self._unslice_potentials(u))
        }

        return


    def __getitem__(self, name):
        return CategoricalMarginal(graph=self, name=name)


# @attr.s(frozen=True, slots=True)
# class CategoricalMarginal():


#     graph = attr.ib()
#     name = attr.ib()


#     def get_moments(self):
#         return [self.graph.get_moments()[self.name]]


#     def marginalize(self):