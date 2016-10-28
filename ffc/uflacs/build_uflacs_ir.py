# -*- coding: utf-8 -*-
# Copyright (C) 2011-2016 Martin Sandve Alnæs
#
# This file is part of UFLACS.
#
# UFLACS is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# UFLACS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with UFLACS. If not, see <http://www.gnu.org/licenses/>.

"""Main algorithm for building the uflacs intermediate representation."""

import numpy
from collections import defaultdict, namedtuple
from itertools import chain

from ufl import product, as_ufl
from ufl.log import error, warning, debug
from ufl.checks import is_cellwise_constant
from ufl.classes import CellCoordinate, FacetCoordinate, QuadratureWeight
from ufl.measure import custom_integral_types, point_integral_types, facet_integral_types
from ufl.algorithms.analysis import has_type

from ffc.uflacs.analysis.balancing import balance_modifiers
from ffc.uflacs.analysis.modified_terminals import is_modified_terminal, analyse_modified_terminal
from ffc.uflacs.analysis.graph import build_graph
from ffc.uflacs.analysis.graph_vertices import build_scalar_graph_vertices
from ffc.uflacs.analysis.graph_rebuild import rebuild_with_scalar_subexpressions
from ffc.uflacs.analysis.dependencies import compute_dependencies, mark_active, mark_image
from ffc.uflacs.analysis.graph_ssa import compute_dependency_count, invert_dependencies
#from ffc.uflacs.analysis.graph_ssa import default_cache_score_policy, compute_cache_scores, allocate_registers
from ffc.uflacs.analysis.factorization import compute_argument_factorization
from ffc.uflacs.elementtables import build_optimized_tables, equal_tables


# Some quick internal structs, massive improvement to
# readability and maintainability over just tuples...

piecewise_ttypes = ("piecewise", "fixed", "ones", "zeros")
uniform_ttypes = ("uniform", "fixed", "ones", "zeros")

table_data_t = namedtuple(
    "table_data_t",
    ["name", "begin", "end", "ttype", "is_piecewise", "is_uniform"]
    )

ma_data_t = namedtuple(
    "ma_data_t",
    ["ma_index", "tabledata"]
    )

common_block_data_fields = ["block_mode", "ttypes", "factor_index", "factor_is_piecewise", "unames", "restrictions"]
common_block_data_t = namedtuple(
    "common_block_data_t",
    common_block_data_fields
    )

def get_common_block_data(blockdata):
    return common_block_data_t(*blockdata[:len(common_block_data_fields)])

preintegrated_block_data_t = namedtuple(
    "preintegrated_block_data_t",
    common_block_data_fields + ["name"]
    )

premultiplied_block_data_t = namedtuple(
    "premultiplied_block_data_t",
    common_block_data_fields + ["name"]
    )

partial_block_data_t = namedtuple(
    "partial_block_data_t",
    common_block_data_fields + ["ma_data", "piecewise_ma_index"]
    )

full_block_data_t = namedtuple(
    "full_block_data_t",
    common_block_data_fields + ["ma_data"]
    )


def multiply_block(point_index, unames, ttypes, unique_tables, table_num_dofs):
    tables = [unique_tables.get(name) for name in unames]
    num_entities = max([1] + [tbl.shape[0] for tbl in tables if tbl is not None])
    num_dofs = tuple(table_num_dofs[name] for name in unames)

    ptable = numpy.zeros((num_entities,) + num_dofs)
    for entity in range(num_entities):
        vectors = []
        for i, tbl in enumerate(tables):
            if tbl is None:
                assert ttypes[i] == "ones"
                vectors.append(numpy.ones((num_dofs[i],)))
            else:
                # Some tables are compacted along entities or points
                e = 0 if tbl.shape[0] == 1 else entity
                q = 0 if tbl.shape[1] == 1 else point_index
                vectors.append(tbl[e, q, :])
        if len(vectors) > 1:
            ptable[entity, ...] = numpy.outer(*vectors)
        elif len(vectors) == 1:
            ptable[entity, :] = vectors[0]
        else:
            error("Nothing to multiply!")
    return ptable


def integrate_block(weights, unames, ttypes, unique_tables, table_num_dofs):
    tables = [unique_tables.get(name) for name in unames]
    num_entities = max([1] + [tbl.shape[0] for tbl in tables if tbl is not None])
    num_dofs = tuple(table_num_dofs[name] for name in unames)

    ptable = numpy.zeros((num_entities,) + num_dofs)
    for iq, w in enumerate(weights):
        ptable[...] += w * multiply_block(iq, unames, ttypes, unique_tables, table_num_dofs)
    return ptable


def empty_expr_ir():
    expr_ir = {}
    expr_ir["V"] = []
    expr_ir["V_active"] = []
    expr_ir["V_targets"] = []
    expr_ir["V_table_data"] = []
    expr_ir["modified_arguments"] = []
    expr_ir["preintegrated_blocks"] = {}
    expr_ir["premultiplied_blocks"] = {}
    expr_ir["preintegrated_contributions"] = defaultdict(list)
    expr_ir["block_contributions"] = defaultdict(list)
    return expr_ir


def build_uflacs_ir(cell, integral_type, entitytype,
                    integrands, tensor_shape,
                    coefficient_numbering,
                    quadrature_rules, parameters):
    # The intermediate representation dict we're building and returning here
    ir = {}

    # FIXME get from parameters:
    epsilon = 1e-10
    #do_apply_preintegration = False
    do_apply_preintegration = True

    # { ufl coefficient: count }
    ir["coefficient_numbering"] = coefficient_numbering

    # Shared unique tables for all quadrature loops
    ir["unique_tables"] = {}
    ir["unique_table_types"] = {}

    # Shared piecewise expr_ir for all quadrature loops
    ir["piecewise_ir"] = empty_expr_ir()

    # { num_points: expr_ir for one integrand }
    ir["varying_irs"] = {}

    # Temporary data structures to build shared piecewise data
    pe2i = {}
    piecewise_modified_argument_indices = {}

    # Whether we expect the quadrature weight to be applied or not
    # (in some cases it's just set to 1 in ufl integral scaling)
    tdim = cell.topological_dimension()
    expect_weight = (
        integral_type not in ("expression",) + point_integral_types
        and (entitytype == "cell"
            or (entitytype == "facet" and tdim > 1)
            or (integral_type in custom_integral_types)
            )
        )

    if integral_type == "expression":
        # TODO: Figure out how to get non-integrand expressions in here, this is just a draft:
        # Analyse all expressions in one list
        assert isinstance(integrands, (tuple, list))
        all_num_points = [None]
        cases = [(None, integrands)]
    else:
        # Analyse each num_points/integrand separately
        assert isinstance(integrands, dict)
        all_num_points = sorted(integrands.keys())
        cases = [(num_points, [integrands[num_points]])
                 for num_points in all_num_points]
    ir["all_num_points"] = all_num_points

    for num_points, expressions in cases:
        # Rebalance order of nested terminal modifiers
        expressions = [balance_modifiers(expr) for expr in expressions]

        # Build initial scalar list-based graph representation
        V, V_deps, V_targets = build_scalar_graph(expressions)


        # Build terminal_data from V here before factorization.
        # Then we can use it to derive table properties for all modified terminals,
        # and then use that to rebuild the scalar graph more efficiently before
        # argument factorization. We can build terminal_data again after factorization
        # if that's necessary.
        initial_terminal_indices = [i for i, v in enumerate(V)
                                    if is_modified_terminal(v)]
        initial_terminal_data = [analyse_modified_terminal(V[i])
                                 for i in initial_terminal_indices]
        unique_tables, mt_table_ranges, table_types, table_num_dofs = \
            build_optimized_tables(num_points, quadrature_rules,
                cell, integral_type, entitytype, initial_terminal_data,
                ir["unique_tables"], parameters)

        # Replace some scalar modified terminals before reconstructing expressions
        # (could possibly use replace() on target expressions instead)
        z = as_ufl(0.0)
        one = as_ufl(1.0)
        for i, mt in zip(initial_terminal_indices, initial_terminal_data):
            if isinstance(mt.terminal, QuadratureWeight):
                # Replace quadrature weight with 1.0, will be added back later
                V[i] = one
            else:
                # Set modified terminals with zero tables to zero
                tr = mt_table_ranges.get(mt)
                if tr is not None:
                    uname, begin, end = tr
                    ttype = table_types[uname]
                    if ttype == "zeros":
                        V[i] = z

        # Propagate expression changes using dependency list
        for i in range(len(V)):
            deps = [V[j] for j in V_deps[i]]
            if deps:
                V[i] = V[i]._ufl_expr_reconstruct_(*deps)

        # Rebuild scalar target expressions and graph
        # (this may be overkill and possible to optimize
        # away if it turns out to be costly)
        expressions = [V[i] for i in V_targets]

        # Rebuild scalar list-based graph representation
        SV, SV_deps, SV_targets = build_scalar_graph(expressions)
        assert all(i < len(SV) for i in SV_targets)


        # Compute factorization of arguments
        (argument_factorizations, modified_arguments,
             FV, FV_deps, FV_targets) = \
            compute_argument_factorization(SV, SV_deps, SV_targets, len(tensor_shape))
        assert len(SV_targets) == len(argument_factorizations)       

        # TODO: Still expecting one target variable in code generation
        assert len(argument_factorizations) == 1
        argument_factorization, = argument_factorizations

        # Store modified arguments in analysed form
        for i in range(len(modified_arguments)):
            modified_arguments[i] = analyse_modified_terminal(modified_arguments[i])

        # Build set of modified_terminal indices into factorized_vertices
        modified_terminal_indices = [i for i, v in enumerate(FV)
                                     if is_modified_terminal(v)]

        # Build set of modified terminal ufl expressions
        modified_terminals = [analyse_modified_terminal(FV[i])
                              for i in modified_terminal_indices]

        # Organize table data more, split into arguments and other terminals
        modified_terminal_table_ranges = [mt_table_ranges.get(mt)
                                          for mt in modified_terminals]
        modified_argument_table_ranges = [mt_table_ranges.get(mt)
                                          for mt in modified_arguments]

        # Dependency analysis
        inv_FV_deps, FV_active, FV_piecewise, FV_varying = \
            analyse_dependencies(FV, FV_deps, FV_targets,
                                 modified_terminal_indices,
                                 mt_table_ranges,
                                 table_types)

        # Mark active modified arguments
        #active_modified_arguments = numpy.zeros(len(modified_arguments), dtype=int)
        #for ma_indices in argument_factorization:
        #    for j in ma_indices:
        #        active_modified_arguments[j] = 1

        # Just some data flow workarounds, V_table_data[i] is passed
        # as argument to backend access and definition together with V[i]
        V_table_data = numpy.empty(len(FV), dtype=object)
        #modified_terminal_table_data = {}  # TODO: This could replace V_table_data
        for i, mt, tr in zip(modified_terminal_indices, modified_terminals, modified_terminal_table_ranges):
            if tr is None:
                td = None
            else:
                name = tr[0]
                ttype = table_types[name]
                td = table_data_t(tr[0], tr[1], tr[2], ttype,
                                  ttype in piecewise_ttypes,
                                  ttype in uniform_ttypes)
                #modified_terminal_table_data[mt] = td
            V_table_data[i] = td

        # Extend piecewise V with unique new FV_piecewise vertices
        pir = ir["piecewise_ir"]
        for i, v in enumerate(FV):
            if FV_piecewise[i]:
                j = pe2i.get(v)
                if j is None:
                    j = len(pe2i)
                    pe2i[v] = j
                    pir["V"].append(v)
                    pir["V_active"].append(1)
                    pir["V_table_data"].append(V_table_data[i])

        # Extend piecewise modified_arguments list with unique new items
        for mt in modified_arguments:
            ma = piecewise_modified_argument_indices.get(mt)
            if ma is None:
                ma = len(pir["modified_arguments"])
                pir["modified_arguments"].append(mt)
                piecewise_modified_argument_indices[mt] = ma

        # Loop over factorization terms
        block_contributions = defaultdict(list)
        for ma_indices, fi in sorted(argument_factorization.items()):
            # Get a bunch of information about this term
            rank = len(ma_indices)
            trs = tuple(modified_argument_table_ranges[ai] for ai in ma_indices)
            unames = tuple(tr[0] for tr in trs)
            dofblock = tuple(tr[1:3] for tr in trs)
            ttypes = tuple(table_types[name] for name in unames)
            assert not any(tt == "zeros" for tt in ttypes)

            # Slightly awkward tuple rearranging to make things cleaner in integralgenerator
            tds = tuple(table_data_t(tr[0], tr[1], tr[2], ttype,
                                     ttype in piecewise_ttypes,
                                     ttype in uniform_ttypes)
                        for tr, ttype in zip(trs, ttypes))

            # Collect relevant restrictions to identify blocks
            # correctly in interior facet integrals
            block_restrictions = []
            for i, ma in enumerate(ma_indices):
                if tds[i].is_uniform:
                    r = None
                else:
                    r = modified_arguments[ma].restriction
                block_restrictions.append(r)
            block_restrictions = tuple(block_restrictions)

            # Store piecewise status for fi and translate
            # index to piecewise scope if relevant
            factor_is_piecewise = FV_piecewise[fi]
            if factor_is_piecewise:
                factor_index = pe2i[FV[fi]]
            else:
                factor_index = fi

            # TODO: Not reusing transposed blocks anywhere

            # Decide out how to handle code generation for this dofblock
            if not do_apply_preintegration:
                # Use full runtime integration by default
                block_mode = "safe"
            else:
                skip_preintegrated = point_integral_types + custom_integral_types + ("interior_facet",)
                skip_premultiplied = point_integral_types + custom_integral_types + ("interior_facet",)
                if (factor_is_piecewise
                        and rank > 0
                        and "quadrature" not in ttypes
                        and integral_type not in skip_preintegrated):
                    # - Piecewise factor is an absolute prerequisite
                    # - Could work for rank 0 as well but currently doesn't
                    # - Haven't considered how quadrature elements work out
                    # - Facet integrals haven't been priority at first,
                    #   integration for each entity adds a little complexity,
                    #   even more so for interior integrals
                    block_mode = "preintegrated"
                elif (rank > 0
                          and all(tt in piecewise_ttypes for tt in ttypes)
                          and integral_type not in skip_premultiplied):
                    # Integrate functional in quadloop, scale block after quadloop
                    block_mode = "premultiplied"
                elif rank == 2 and any(tt in piecewise_ttypes for tt in ttypes):
                    # Partial computation in quadloop of f*u[i],
                    # compute (f*u[i])*v[i] outside quadloop,
                    # (or with u,v swapped)
                    block_mode = "partial"
                else:
                    # Full runtime integration of f*u[i]*v[j],
                    # can still do partial computation in quadloop of f*u[i]
                    # but must compute (f*u[i])*v[i] as well inside quadloop.
                    # (or with u,v swapped)
                    block_mode = "full"

            # Carry out decision
            if block_mode == "preintegrated":
                # Add to contributions:
                # P = sum_q weight*u*v;      preintegrated here
                # B[...] = f * P[...];       generated after quadloop
                # A[dofblock] += B[...];     generated after quadloop

                # TODO: Reuse transpose to save memory
                cache = ir["piecewise_ir"]["preintegrated_blocks"]
                pname = cache.get(unames)
                if pname is None:
                    weights = quadrature_rules[num_points][1]
                    ptable = integrate_block(weights, unames, ttypes, unique_tables, table_num_dofs)
                    pname = "PI%d" % (len(cache,))
                    cache[unames] = pname
                    unique_tables[pname] = ptable
                    table_types[pname] = "preintegrated"

                assert factor_is_piecewise
                block_unames = (pname,)
                blockdata = preintegrated_block_data_t(block_mode, ttypes,
                                                       factor_index, factor_is_piecewise,
                                                       block_unames, block_restrictions,
                                                       pname)
                block_is_piecewise = True

            elif block_mode == "premultiplied":
                # Add to contributions:
                # P = u*v;                        computed here
                # FI = sum_q weight * f;          generated inside quadloop
                # B[...] = FI * P[...];           generated after quadloop
                # A[dofblock] += B[...];          generated after quadloop

                # TODO: Reuse transpose to save memory
                cache = ir["piecewise_ir"]["premultiplied_blocks"]
                pname = cache.get(unames)
                if pname is None:
                    ptable = multiply_block(0, unames, ttypes, unique_tables, table_num_dofs)
                    pname = "PM%d" % (len(cache,))
                    cache[unames] = pname
                    unique_tables[pname] = ptable
                    table_types[pname] = "premultiplied"

                assert not factor_is_piecewise
                block_unames = (pname,)
                blockdata = premultiplied_block_data_t(block_mode, ttypes,
                                                       factor_index, factor_is_piecewise,
                                                       block_unames, block_restrictions,
                                                       pname)
                block_is_piecewise = False

            elif block_mode in ("partial", "full", "safe"):
                # Translate indices to piecewise context if necessary
                block_is_piecewise = factor_is_piecewise and not expect_weight
                ma_data = []
                for i, ma in enumerate(ma_indices):
                    if tds[i].is_piecewise:
                        ma_index = piecewise_modified_argument_indices[modified_arguments[ma]]
                    else:
                        block_is_piecewise = False
                        ma_index = ma
                    ma_data.append(ma_data_t(ma_index, tds[i]))

                if block_mode == "partial":
                    # Add to contributions:
                    # P[i] = sum_q weight * f * u[i];  generated inside quadloop
                    # B[i,j] = P[i] * v[j];            generated after quadloop (where v is the piecewise ma)
                    # A[dofblock] += B[...];           generated after quadloop

                    # Find first piecewise index TODO: Is last better? just reverse range here
                    for i in range(rank):
                        if tds[i].is_piecewise:
                            piecewise_ma_index = i
                            break
                    assert rank == 2
                    not_piecewise_ma_index = 1 - piecewise_ma_index
                    block_unames = (unames[not_piecewise_ma_index],)
                    blockdata = partial_block_data_t(block_mode,  ttypes,
                                                     factor_index, factor_is_piecewise,
                                                     block_unames, block_restrictions,
                                                     tuple(ma_data), piecewise_ma_index)
                elif block_mode in ("full", "safe"):
                    # Add to contributions:
                    # B[i] = sum_q weight * f * u[i] * v[j];  generated inside quadloop
                    # A[dofblock] += B[i];                    generated after quadloop

                    block_unames = unames
                    blockdata = full_block_data_t(block_mode, ttypes,
                                                  factor_index, factor_is_piecewise,
                                                  block_unames, block_restrictions,
                                                  tuple(ma_data))
            else:
                error("Invalid block_mode %s" % (block_mode,))

            if block_is_piecewise:
                # Insert in piecewise expr_ir
                ir["piecewise_ir"]["block_contributions"][dofblock].append(blockdata)
            else:
                # Insert in varying expr_ir for this quadrature loop
                block_contributions[dofblock].append(blockdata)

        # Figure out which table names are referenced in unstructured partition
        active_table_names = set()
        for i, tr in zip(modified_terminal_indices, modified_terminal_table_ranges):
            if FV_active[i] and tr is not None:
                active_table_names.add(tr[0])

        # Figure out which table names are referenced in blocks
        for dofblock, contributions in chain(block_contributions.items(),
                                             ir["piecewise_ir"]["block_contributions"].items()):
            for blockdata in contributions:
                if blockdata.block_mode in ("preintegrated", "premultiplied"):
                    active_table_names.add(blockdata.name)
                elif blockdata.block_mode in ("partial", "full", "safe"):
                    for mad in blockdata.ma_data:
                        active_table_names.add(mad.tabledata.name)

        # Record all table types before dropping tables
        ir["unique_table_types"].update(table_types)

        # Drop tables not referenced from modified terminals
        # and tables of zeros and ones
        unused_ttypes = ("zeros", "ones", "quadrature")
        keep_table_names = set()
        for name in active_table_names:
            ttype = ir["unique_table_types"][name]
            if ttype not in unused_ttypes:
                if name in unique_tables:
                    keep_table_names.add(name)
        unique_tables = { name: unique_tables[name]
                          for name in keep_table_names }

        # Add to global set of all tables
        for name, table in unique_tables.items():
            tbl = ir["unique_tables"].get(name)
            if tbl is not None and not equal_tables(tbl, table, epsilon):
                error("Table values mismatch with same name.")
        ir["unique_tables"].update(unique_tables)

        # Analyse active terminals to check what we'll need to generate code for
        active_mts = []
        for i, mt in zip(modified_terminal_indices, modified_terminals):
            if FV_active[i]:
                active_mts.append(mt)

        # Figure out if we need to access CellCoordinate to
        # avoid generating quadrature point table otherwise
        if integral_type == "cell":
            need_points = any(isinstance(mt.terminal, CellCoordinate)
                              for mt in active_mts)
        elif integral_type in facet_integral_types:
            need_points = any(isinstance(mt.terminal, FacetCoordinate)
                              for mt in active_mts)
        else:
            need_points = False

        # Figure out if we need to access QuadratureWeight to
        # avoid generating quadrature point table otherwise
        #need_weights = any(isinstance(mt.terminal, QuadratureWeight)
        #                   for mt in active_mts)

        # Count blocks of each mode
        block_modes = defaultdict(int)
        for dofblock, contributions in block_contributions.items():
            for blockdata in contributions:
                block_modes[blockdata.block_mode] += 1
        # Debug output
        summary = "\n".join("  %d\t%s" % (count, mode)
                            for mode, count in sorted(block_modes.items()))
        debug("Blocks of each mode: \n" + summary)

        # If there are any blocks other than preintegrated we need weights
        if expect_weight and any(mode != "preintegrated" for mode in block_modes):
            need_weights = True
        else:
            need_weights = False

        # Build IR dict for the given expressions
        expr_ir = {}

        # (array) FV-index -> UFL subexpression
        expr_ir["V"] = FV

        # (array) V indices for each input expression component in flattened order
        expr_ir["V_targets"] = FV_targets

        ### Result of factorization:
        # (array) MA-index -> UFL expression of modified arguments
        expr_ir["modified_arguments"] = modified_arguments

        # (dict) tuple(MA-indices) -> FV-index of monomial factor
        #expr_ir["argument_factorization"] = argument_factorization

        expr_ir["block_contributions"] = block_contributions

        ### Modified terminals
        # (array) list of FV-indices to modified terminals
        #expr_ir["modified_terminal_indices"] = modified_terminal_indices

        # Dependency structure of graph:
        # (CRSArray) FV-index -> direct dependency FV-index list
        #expr_ir["dependencies"] = FV_deps

        # (CRSArray) FV-index -> direct dependee FV-index list
        #expr_ir["inverse_dependencies"] = inv_FV_deps

        # Metadata about each vertex
        #expr_ir["active"] = FV_active        # (array) FV-index -> bool
        #expr_ir["V_piecewise"] = FV_piecewise  # (array) FV-index -> bool
        expr_ir["V_varying"] = FV_varying      # (array) FV-index -> bool

        #expr_ir["modified_terminal_table_ranges"] = modified_terminal_table_ranges
        #expr_ir["modified_argument_table_ranges"] = modified_argument_table_ranges

        # Store mapping from modified terminal object to
        # table data, this is used in integralgenerator
        #expr_ir["modified_terminal_table_data"] = modified_terminal_table_data

        # Store table data in FV indexing, this is used in integralgenerator
        expr_ir["V_table_data"] = V_table_data

        # To emit quadrature rules only if needed
        expr_ir["need_points"] = need_points
        expr_ir["need_weights"] = need_weights

        # Store the tables and ranges
        #expr_ir["table_types"] = table_types
        #expr_ir["unique_tables"] = unique_tables

        # Store final ir for this num_points
        ir["varying_irs"][num_points] = expr_ir

    return ir


def build_scalar_graph(expressions):
    """Build list representation of expression graph covering the given expressions.

    TODO: Renaming, refactoring and cleanup of the graph building algorithms used in here
    """

    # Build the initial coarse computational graph of the expression
    G = build_graph(expressions)

    assert len(expressions) == 1, "FIXME: Multiple expressions in graph building needs more work from this point on."

    # Build more fine grained computational graph of scalar subexpressions
    # TODO: Make it so that
    #   expressions[k] <-> NV[nvs[k][:]],
    #   len(nvs[k]) == value_size(expressions[k])
    scalar_expressions = rebuild_with_scalar_subexpressions(G)

    # Sanity check on number of scalar symbols/components
    assert len(scalar_expressions) == sum(product(expr.ufl_shape) for expr in expressions)

    # Build new list representation of graph where all
    # vertices of V represent single scalar operations
    e2i, V, V_targets = build_scalar_graph_vertices(scalar_expressions)

    # Compute sparse dependency matrix
    V_deps = compute_dependencies(e2i, V)

    return V, V_deps, V_targets


def analyse_dependencies(V, V_deps, V_targets,
                         modified_terminal_indices,
                         mt_table_ranges,
                         table_types):
    # Count the number of dependencies every subexpr has
    V_depcount = compute_dependency_count(V_deps)

    # Build the 'inverse' of the sparse dependency matrix
    inv_deps = invert_dependencies(V_deps, V_depcount)

    # Mark subexpressions of V that are actually needed for final result
    active, num_active = mark_active(V_deps, V_targets)

    # Build piecewise/varying markers for factorized_vertices
    varying_indices = []
    for i in modified_terminal_indices:

        # TODO: Can probably avoid this re-analysis by
        # passing other datastructures in here:
        mt = analyse_modified_terminal(V[i])
        tr = mt_table_ranges.get(mt)
        if tr is not None:
            # Check if table computations have revealed values varying over points
            uname = tr[0]
            ttype = table_types[uname]
            # Note: uniform means entity-wise uniform, varying over points
            if ttype in ("varying", "uniform", "quadrature"):
                varying_indices.append(i)
            else:
                if ttype not in ("fixed", "piecewise", "ones", "zeros"):
                    error("Invalid ttype %s" % (ttype,))

        elif not is_cellwise_constant(V[i]):
            # Keeping this check to be on the safe side,
            # not sure which cases this will cover (if any)
            varying_indices.append(i)

    # Mark every subexpression that is computed
    # from the spatially dependent terminals
    varying, num_varying = mark_image(inv_deps, varying_indices)

    # The rest of the subexpressions are piecewise constant (1-1=0, 1-0=1)
    piecewise = 1 - varying

    # Unmark non-active subexpressions
    varying *= active
    piecewise *= active

    # TODO: Skip literals in both varying and piecewise
    # nonliteral = ...
    # varying *= nonliteral
    # piecewise *= nonliteral

    return inv_deps, active, piecewise, varying


# TODO: Consider comments below and do it or delete them.

""" Old comments:

Work for later::

        - Apply some suitable renumbering of vertices and corresponding arrays prior to returning

        - Allocate separate registers for each partition
          (but e.g. argument[iq][i0] may need to be accessible in other loops)

        - Improve register allocation algorithm

        - Take a list of expressions as input to compile several expressions in one joined graph
          (e.g. to compile a,L,M together for nonlinear problems)

"""


""" # Old comments:

    # TODO: Inspection of varying shows that factorization is
    # needed for effective loop invariant code motion w.r.t. quadrature loop as well.
    # Postphoning that until everything is working fine again.
    # Core ingredients for such factorization would be:
    # - Flatten products of products somehow
    # - Sorting flattened product factors by loop dependency then by canonical ordering
    # Or to keep binary products:
    # - Rebalancing product trees ((a*c)*(b*d) -> (a*b)*(c*d)) to make piecewise quantities 'float' to the top of the list

    # rank = max(len(ma_indices) for ma_indices in argument_factorization)
    # for i,a in enumerate(modified_arguments):
    #    iarg = a.number()
    # ipart = a.part()

    # TODO: More structured MA organization?
    #modified_arguments[rank][block][entry] -> UFL expression of modified argument
    #dofranges[rank][block] -> (begin, end)
    # or
    #modified_arguments[rank][entry] -> UFL expression of modified argument
    #dofrange[rank][entry] -> (begin, end)
    #argument_factorization: (dict) tuple(MA-indices (only relevant ones!)) -> V-index of monomial factor
    # becomes
    #argument_factorization: (dict) tuple(entry for each(!) rank) -> V-index of monomial factor ## doesn't cover intermediate f*u in f*u*v!
"""


"""
def old_code_useful_for_optimization():

    # Use heuristics to mark the usefulness of storing every subexpr in a variable
    scores = compute_cache_scores(V,
                                  active,
                                  dependencies,
                                  inverse_dependencies,
                                  partitions,  # TODO: Rewrite in terms of something else, this doesn't exist anymore
                                  cache_score_policy=default_cache_score_policy)

    # Allocate variables to store subexpressions in
    allocations = allocate_registers(active, partitions, target_variables,
                                     scores, int(parameters["max_registers"]), int(parameters["score_threshold"]))
    target_registers = [allocations[r] for r in target_variables]
    num_registers = sum(1 if x >= 0 else 0 for x in allocations)
    # TODO: If we renumber we can allocate registers separately for each partition, which is probably a good idea.

    expr_oir = {}
    expr_oir["num_registers"] = num_registers
    expr_oir["partitions"] = partitions
    expr_oir["allocations"] = allocations
    expr_oir["target_registers"] = target_registers
    return expr_oir
"""

