# -*- coding: utf-8 -*-
# Copyright (C) 2011-2015 Martin Sandve Alnæs
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

"""Definitions of 'modified terminals', a core concept in uflacs."""

from __future__ import print_function # used in some debugging

from six.moves import zip
from ufl.permutation import build_component_numbering
from ufl.classes import (FormArgument, Argument,
                         Indexed, FixedIndex,
                         ReferenceValue,
                         Grad, ReferenceGrad,
                         Restricted,
                         FacetAvg, CellAvg)

from ffc.log import error
from ffc.log import ffc_assert


class ModifiedTerminal(object):

    """A modified terminal expression is an object of a Terminal subtype, wrapped in terminal modifier types.

    The variables of this class are:

        expr - The original UFL expression
        terminal           - the underlying Terminal object

        global_derivatives - tuple of ints, each meaning derivative in that global direction
        local_derivatives  - tuple of ints, each meaning derivative in that local direction
        reference_value    - bool, whether this is represented in reference frame
        averaged           - None, 'facet' or 'cell'
        restriction        - None, '+' or '-'

        component          - tuple of ints, the global component of the Terminal
        flat_component     - single int, flattened local component of the Terminal, considering symmetry


        Possibly other component model:
        - global_component
        - reference_component
        - flat_component

    """
    def __init__(self, expr, terminal,
                 reference_value, base_shape,
                 component, flat_component,
                 global_derivatives, local_derivatives,
                 averaged, restriction):
        # The original expression
        self.expr = expr

        # The underlying terminal expression
        self.terminal = terminal

        # Are we seeing the terminal in physical or reference frame
        self.reference_value = reference_value

        # Get the shape of the core terminal or its reference value,
        # this is the shape that component and flat_component refers to
        self.base_shape = base_shape

        # Components
        self.component = component
        self.flat_component = flat_component

        # Derivatives
        self.global_derivatives = global_derivatives
        self.local_derivatives = local_derivatives

        # Evaluation method (alternatives: { None, 'facet_midpoint',
        #  'cell_midpoint', 'facet_avg', 'cell_avg' })
        self.averaged = averaged

        # Restriction to one cell or the other for interior facet integrals
        self.restriction = restriction

    def as_tuple(self):
        """Return a tuple with sortable values that uniquely identifies this modified terminal.

        Some of the derived variables are omitted.
        """
        t = self.terminal  # FIXME: Terminal is not sortable...
        rv = self.reference_value
        #bs = self.base_shape
        #c = self.component
        fc = self.flat_component
        gd = self.global_derivatives
        ld = self.local_derivatives
        a = self.averaged
        r = self.restriction
        return (t, rv, fc, gd, ld, a, r)

    def argument_ordering_key(self):
        """Return a key for deterministic sorting of argument vertex
        indices based on the properties of the modified terminal.
        Used in factorization but moved here for closeness with ModifiedTerminal attributes."""
        t = self.terminal
        assert isinstance(t, Argument)
        n = t.number()
        assert n >= 0
        p = t.part()
        rv = self.reference_value
        #bs = self.base_shape
        #c = self.component
        fc = self.flat_component
        gd = self.global_derivatives
        ld = self.local_derivatives
        a = self.averaged
        r = self.restriction
        return (n, p, rv, fc, gd, ld, a, r)

    def __hash__(self):
        return hash(self.as_tuple())

    def __eq__(self, other):
        return isinstance(other, ModifiedTerminal) and self.as_tuple() == other.as_tuple()

    def __lt__(self, other):
        return self.as_tuple() < other.as_tuple()  # FIXME: Terminal is not sortable...

    def __str__(self):
        s = []
        s += ["terminal:           {0}".format(self.terminal)]
        s += ["global_derivatives: {0}".format(self.global_derivatives)]
        s += ["local_derivatives:  {0}".format(self.local_derivatives)]
        s += ["averaged:           {0}".format(self.averaged)]
        s += ["component:          {0}".format(self.component)]
        s += ["restriction:        {0}".format(self.restriction)]
        return '\n'.join(s)


def is_modified_terminal(v):
    "Check if v is a terminal or a terminal wrapped in terminal modifier types."
    while not v._ufl_is_terminal_:
        if v._ufl_is_terminal_modifier_:
            v = v.ufl_operands[0]
        else:
            return False
    return True

def strip_modified_terminal(v):
    "Extract core Terminal from a modified terminal or return None."
    while not v._ufl_is_terminal_:
        if v._ufl_is_terminal_modifier_:
            v = v.ufl_operands[0]
        else:
            return None
    return v


def analyse_modified_terminal(expr):
    """Analyse a so-called 'modified terminal' expression.

    Return its properties in more compact form as a ModifiedTerminal object.

    A modified terminal expression is an object of a Terminal subtype,
    wrapped in terminal modifier types.

    The wrapper types can include 0-* Grad or ReferenceGrad objects,
    and 0-1 ReferenceValue, 0-1 Restricted, 0-1 Indexed,
    and 0-1 FacetAvg or CellAvg objects.
    """
    # Data to determine
    component = None
    global_derivatives = []
    local_derivatives = []
    reference_value = None
    restriction = None
    averaged = None

    # Start with expr and strip away layers of modifiers
    t = expr
    while not t._ufl_is_terminal_:
        if isinstance(t, Indexed):
            ffc_assert(component is None, "Got twice indexed terminal.")

            t, i = t.ufl_operands
            component = [int(j) for j in i]

            ffc_assert(all(isinstance(j, FixedIndex) for j in i), "Expected only fixed indices.")

        elif isinstance(t, ReferenceValue):
            ffc_assert(reference_value is None, "Got twice pulled back terminal!")

            t, = t.ufl_operands
            reference_value = True

        elif isinstance(t, ReferenceGrad):
            ffc_assert(len(component), "Got local gradient of terminal without prior indexing.")

            t, = t.ufl_operands
            local_derivatives.append(component[-1])
            component = component[:-1]

        elif isinstance(t, Grad):
            ffc_assert(len(component), "Got gradient of terminal without prior indexing.")

            t, = t.ufl_operands
            global_derivatives.append(component[-1])
            component = component[:-1]

        elif isinstance(t, Restricted):
            ffc_assert(restriction is None, "Got twice restricted terminal!")

            restriction = t._side
            t, = t.ufl_operands

        elif isinstance(t, CellAvg):
            ffc_assert(averaged is None, "Got twice averaged terminal!")

            t, = t.ufl_operands
            averaged = "cell"

        elif isinstance(t, FacetAvg):
            ffc_assert(averaged is None, "Got twice averaged terminal!")

            t, = t.ufl_operands
            averaged = "facet"

        elif t._ufl_terminal_modifiers_:
            error("Missing handler for terminal modifier type %s, object is %s." % (type(t), repr(t)))

        else:
            error("Unexpected type %s object %s." % (type(t), repr(t)))


    # Make canonical representation of derivatives
    global_derivatives = tuple(sorted(global_derivatives))
    local_derivatives = tuple(sorted(local_derivatives))

    # TODO: Temporarily letting local_derivatives imply reference_value,
    #       but this was not intended to be the case
    #if local_derivatives:
    #    reference_value = True

    # Make reference_value true or false
    reference_value = reference_value or False

    # Consistency check
    if local_derivatives and not reference_value:
        error("Local derivatives of non-local value is not legal.")
    if global_derivatives and reference_value:
        error("Global derivatives of local value is not legal.")


    # Make sure component is an integer tuple
    if component is None:
        component = ()
    else:
        component = tuple(component)

    # Get the shape of the core terminal or its reference value,
    # this is the shape that component refers to
    element = t.ufl_element()
    if reference_value:
        base_shape = element.reference_value_shape()
        # Ignoring symmetry, assuming already applied in conversion to reference frame
        assert len(base_shape) <= 1
        symmetry = {}
    else:
        base_shape = t.ufl_shape
        if isinstance(t, FormArgument):
            symmetry = t.ufl_element().symmetry()
        else:
            symmetry = {}

    # Assert that component is within the shape of the (reference) terminal
    ffc_assert(len(component) == len(base_shape),
               "Length of component does not match rank of (reference) terminal.")
    ffc_assert(all(c >= 0 and c < d for c, d in zip(component, base_shape)),
               "Component indices %s are outside value shape %s" % (component, base_shape))


    # Flatten component
    vi2si, si2vi = build_component_numbering(base_shape, symmetry)
    flat_component = vi2si[component]
    # num_flat_components = len(si2vi)

    return ModifiedTerminal(expr, t,
                            reference_value, base_shape,
                            component, flat_component,
                            global_derivatives, local_derivatives,
                            averaged, restriction)
