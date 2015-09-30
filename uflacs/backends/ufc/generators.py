
import inspect
import re
from string import Formatter

from ufl import product
from ffc.log import error, warning
from ffc.backends.ufc import *

from uflacs.language.format_lines import format_indented_lines
from uflacs.backends.ufc.templates import *

#__all__ = (["ufc_form", "ufc_dofmap", "ufc_finite_element", "ufc_integral"]
#           + ["ufc_%s_integral" % integral_type for integral_type in integral_types])

# These are all the integral types directly supported in ufc.
# TODO: Get these from somewhere for more automation.
ufc_integral_types = ("cell", "exterior_facet", "interior_facet", "vertex", "custom")

# These are the method names in ufc::form that are specialized for each integral type
integral_name_templates = (
    "max_%s_subdomain_id",
    "has_%s_integrals",
    "create_%s_integral",
    "create_default_%s_integral",
    )

# TODO: Move to language utils
def generate_return_new_switch(L, i, classnames):
    if classnames:
        cases = []
        for j, classname in enumerate(classnames):
            if classname:
                cases.append((j, L.Return(L.New(classname))))
        code = [L.Switch(i, cases, autobreak=False, autoscope=False)]
    else:
        code = []
    code.append(L.Return(L.Null()))
    return L.StatementList(code)


class ufc_generator(object):
    """Common functionality for code generators producing ufc classes.

    The generate function is the driver for generating code for a class.
    It automatically extracts template keywords and inserts the results
    from calls to self.<keyword>(language, ir), or the value of ir[keyword]
    if there is no self.<keyword>.
    """
    def __init__(self, header_template, implementation_template):
        self._header_template = header_template
        self._implementation_template = implementation_template

        r = re.compile(r"%\(([a-zA-Z0-9_]*)\)")
        self._header_keywords = set(r.findall(self._header_template))
        self._implementation_keywords = set(r.findall(self._implementation_template))

        self._keywords = sorted(self._header_keywords | self._implementation_keywords)

    def generate_snippets(self, L, ir):
        # Generate code snippets for each keyword found in templates
        snippets = {}
        for kw in self._keywords:
            # Try self.<keyword>(L, ir) if available, otherwise use ir[keyword]
            if hasattr(self, kw):
                method = getattr(self, kw)
                value = method(L, ir)
                if isinstance(value, L.CStatement):
                    value = L.Indented(value.cs_format())
                    value = format_indented_lines(value)
            else:
                value = ir.get(kw)
                if value is None:
                    error("Missing template keyword '%s' in ir for '%s'." % (kw, self.__class__.__name__))
            snippets[kw] = value

        # Error checking (can detect some bugs early when changing the interface)
        valueonly = {"classname"}
        attrs = set(name for name in dir(self) if not name.startswith("_"))
        base_attrs = set(name for name in dir(ufc_generator) if not name.startswith("_"))
        base_attrs.add("generate_snippets")
        base_attrs.add("generate")
        unused = attrs - set(self._keywords) - base_attrs
        missing = set(self._keywords) - attrs - valueonly
        if unused:
            warning("*** Unused generator functions:\n%s" % ('\n'.join(map(str,sorted(unused))),))
        if missing:
            warning("*** Missing generator functions:\n%s" % ('\n'.join(map(str,sorted(missing))),))

        return snippets

    def generate(self, L, ir):
        # Return composition of templates with generated snippets
        snippets = self.generate_snippets(L, ir)
        h = self._header_template % snippets
        cpp = self._implementation_template % snippets
        return h, cpp

    def preamble(self, L, ir):
        "Override in classes that need additional declarations inside class."
        assert not ir.get("preamble")
        return ""

    def members(self, L, ir):
        "Override in classes that need members."
        assert not ir.get("members")
        return ""

    def constructor(self, L, ir):
        "Override in classes that need constructor."
        assert not ir.get("constructor")
        return ""

    def constructor_arguments(self, L, ir):
        "Override in classes that need constructor."
        assert not ir.get("constructor_arguments")
        return ""

    def initializer_list(self, L, ir):
        "Override in classes that need constructor."
        assert not ir.get("")
        return ""

    def destructor(self, L, ir):
        "Override in classes that need destructor."
        assert not ir.get("destructor")
        return ""

    def signature(self, L, ir):
        "Default implementation of returning signature string fetched from ir."
        value = ir["signature"]
        return L.Return(L.LiteralString(value))

    def create(self, L, ir):
        "Default implementation of creating a new object of the same type."
        classname = ir["classname"]
        return L.Return(L.New(classname))

    def topological_dimension(self, L, ir):
        "Default implementation of returning topological dimension fetched from ir."
        value = ir["topological_dimension"]
        return L.Return(L.LiteralInt(value))

    def geometric_dimension(self, L, ir):
        "Default implementation of returning geometric dimension fetched from ir."
        value = ir["geometric_dimension"]
        return L.Return(L.LiteralInt(value))

def add_ufc_form_integral_methods(cls):
    """This function generates methods on the class it decorates, for each integral type.

    This allows implementing e.g. create_###_integrals once in the decorated class,
    while
    """
    # The name "foo" is chosen for familiarity for ffc developers
    impl_type = "foo"

    for template in integral_name_templates:
        implname = "_" + (template % (impl_type,))
        impl = getattr(cls, implname)
        for integral_type in ufc_integral_types:
            declname = template % (integral_type,)

            # Binding variables explicitly because Python closures don't
            # capture the value of integral_type for each iteration here
            def _delegate(self, L, ir, integral_type=integral_type, declname=declname, impl=impl):
                return impl(self, L, ir, integral_type, declname)
            _delegate.__doc__ = impl.__doc__ % {"declname": declname, "integral_type": integral_type}

            setattr(cls, declname, _delegate)
    return cls

@add_ufc_form_integral_methods
class ufc_form(ufc_generator):
    def __init__(self):
        ufc_generator.__init__(self, form_header, form_implementation)

    def num_coefficients(self, L, ir):
        value = ir["num_coefficients"]
        return L.Return(L.LiteralInt(value))

    def rank(self, L, ir):
        value = ir["rank"]
        return L.Return(L.LiteralInt(value))

    # TODO: missing 's' in ufc signature:
    def original_coefficient_position(self, L, ir): # FIXME: port this
        # Input args
        i = L.Symbol("i")
        positions = ir["original_coefficient_positions"]
        code = "FIXME"
        return code

    def create_coordinate_finite_element(self, L, ir):
        classname = ir["create_coordinate_finite_element"] # FIXME: ffc provides element id, not classname
        return L.Return(L.New(classname))
        # TODO: Use factory functions instead, here and in all create_* functions:
        #classname = ir["coordinate_finite_element_classname"] # Not in FFC
        #factoryname = make_factory_function_name(classname)
        #return L.Return(L.Call(factoryname))

    def create_coordinate_dofmap(self, L, ir):
        classname = ir["create_coordinate_dofmap"] # FIXME: ffc provides element id, not classname
        return L.Return(L.New(classname))

    def create_finite_element(self, L, ir):
        i = L.Symbol("i")
        classnames = ir["create_finite_element"] # FIXME: ffc provides element id, not classname
        return generate_return_new_switch(L, i, classnames)

    def create_dofmap(self, L, ir):
        i = L.Symbol("i")
        classnames = ir["create_dofmap"] # FIXME: ffc provides element id, not classname
        return generate_return_new_switch(L, i, classnames)

    def _max_foo_subdomain_id(self, L, ir, integral_type, declname):
        "Return implementation of ufc::form::%(declname)s()."
        value = ir[declname]
        return L.Return(L.LiteralInt(value))

    def _has_foo_integrals(self, L, ir, integral_type, declname):
        "Return implementation of ufc::form::%(declname)s()."
        value = ir[declname]
        return L.Return(L.LiteralBool(value))

    def _create_foo_integral(self, L, ir, integral_type, declname):
        "Return implementation of ufc::form::%(declname)s()."
        subdomain_id = L.Symbol("subdomain_id")
        classnames = ir[declname] # FIXME: ffc provides element id, not classname
        return generate_return_new_switch(L, subdomain_id, classnames)

    def _create_default_foo_integral(self, L, ir, integral_type, declname):
        "Return implementation of ufc::form::%(declname)s()."
        classname = ir[declname] # FIXME: ffc provides element id, not classname
        if classname:
            return L.Return(L.New(classname))
        else:
            return L.Return(L.Null())


class ufc_dofmap(ufc_generator):
    def __init__(self):
        ufc_generator.__init__(self, dofmap_header, dofmap_implementation)

    def needs_mesh_entities(self, L, ir):
        d = L.Symbol("d")
        nme = ir["needs_mesh_entities"]
        cases = [(L.LiteralInt(dim), L.Return(L.LiteralBool(need)))
                 for dim, need in enumerate(nme)]
        default = L.Return(L.LiteralBool(False))
        return L.Switch(d, cases, default=default, autoscope=False, autobreak=False)

    def global_dimension(self, L, ir): # FIXME: port this
        value = ir["global_dimension"] # FIXME: This is not an int
        code = "FIXME"
        return code

    def num_element_dofs(self, L, ir):
        value = ir["num_element_dofs"]
        return L.Return(L.LiteralInt(value))

    def num_facet_dofs(self, L, ir):
        value = ir["num_facet_dofs"]
        return L.Return(L.LiteralInt(value))

    def num_entity_dofs(self, L, ir):
        d = L.Symbol("d")
        values = ir["num_entity_dofs"]
        cases = [(i, L.Return(L.LiteralInt(value))) for i, value in enumerate(values)]
        default = L.Return(L.LiteralInt(0))
        return L.Switch(d, cases, default=default)

    def tabulate_dofs(self, L, ir): # FIXME: port this
        code = "FIXME"
        return code

    def tabulate_facet_dofs(self, L, ir): # FIXME: port this
        code = "FIXME"
        return code

    def tabulate_entity_dofs(self, L, ir): # FIXME: port this
        code = "FIXME"
        return code

    def num_sub_dofmaps(self, L, ir):
        value = ir["num_sub_dofmaps"]
        return L.Return(L.LiteralInt(value))

    def create_sub_dofmap(self, L, ir):
        i = L.Symbol("i")
        classnames = ir["create_sub_dofmap"] # FIXME: ffc provides element ids, not classname
        return generate_return_new_switch(L, i, classnames)


class ufc_finite_element(ufc_generator):
    def __init__(self):
        ufc_generator.__init__(self, finite_element_header, finite_element_implementation)

    def cell_shape(self, L, ir):
        name = ir["cell_shape"]
        return L.Return(L.Symbol(name))

    def space_dimension(self, L, ir):
        value = ir["space_dimension"]
        return L.Return(L.LiteralInt(value))

    def value_rank(self, L, ir):
        sh = ir["value_dimension"]
        return L.Return(L.LiteralInt(len(sh)))

    def value_size(self, L, ir):
        sh = ir["value_dimension"]
        return L.Return(L.LiteralInt(product(sh)))

    def value_dimension(self, L, ir):
        i = L.Symbol("i")
        sh = ir["value_dimension"]
        cases = [(L.LiteralInt(j), L.Return(L.LiteralInt(k))) for j, k in enumerate(sh)]
        default = L.Return(L.LiteralInt(0))
        return L.Switch(i, cases, default=default, autoscope=False, autobreak=False)

    def reference_value_rank(self, L, ir):
        sh = ir["reference_value_dimension"]
        return L.Return(L.LiteralInt(len(sh)))

    def reference_value_size(self, L, ir):
        sh = ir["reference_value_dimension"]
        return L.Return(L.LiteralInt(product(sh)))

    def reference_value_dimension(self, L, ir):
        i = L.Symbol("i")
        sh = ir["reference_value_dimension"]
        cases = [(L.LiteralInt(j), L.Return(L.LiteralInt(k))) for j, k in enumerate(sh)]
        default = L.Return(L.LiteralInt(0))
        return L.Switch(i, cases, default=default, autoscope=False, autobreak=False)

    def evaluate_basis(self, L, ir): # FIXME: port this
        return "FIXME" + ir["evaluate_basis"]

    def evaluate_basis_derivatives(self, L, ir): # FIXME: port this
        return "FIXME" + ir["evaluate_basis_derivatives"]

    def evaluate_basis_all(self, L, ir): # FIXME: port this
        return "FIXME" + ir["evaluate_basis_all"]

    def evaluate_basis_derivatives_all(self, L, ir): # FIXME: port this
        return "FIXME" + ir["evaluate_basis_derivatives_all"]

    def evaluate_dof(self, L, ir): # FIXME: port this
        return "FIXME" + ir["evaluate_dof"]

    def evaluate_dofs(self, L, ir): # FIXME: port this
        return "FIXME" + ir["evaluate_dofs"]

    def interpolate_vertex_values(self, L, ir): # FIXME: port this
        return "FIXME" + ir["interpolate_vertex_values"]

    def tabulate_dof_coordinates(self, L, ir): # FIXME: port this
        coords = ir["tabulate_dof_coordinates"]
        code = "FIXME" + str(coords)
        return code

    def num_sub_elements(self, L, ir):
        n = ir["num_sub_elements"]
        return L.Return(L.LiteralInt(n))

    def create_sub_element(self, L, ir):
        i = L.Symbol("i")
        classnames = ir["create_sub_element"] # FIXME: ffc provides element ids, not classname
        return generate_return_new_switch(L, i, classnames)


class ufc_integral(ufc_generator):
    def __init__(self, integral_type):
        assert integral_type in ufc_integral_types
        integral_header = eval("%s_integral_header" % integral_type)
        integral_implementation = eval("%s_integral_implementation" % integral_type)
        ufc_generator.__init__(self, integral_header, integral_implementation)

    def enabled_coefficients(self, L, ir):
        enabled_coefficients = ir["enabled_coefficients"]
        initializer_list = ", ".join("true" if enabled else "false"
                                     for enabled in enabled_coefficients)
        code = L.StatementList([
            # Cheating a bit with verbatim:
            L.VerbatimStatement("static const std::vector<bool> enabled({%s});" % initializer_list),
            L.Return(L.Symbol("enabled")),
            ])
        return code

    def tabulate_tensor(self, L, ir):
        # FIXME: This is where the current ffc code generation goes
        tt = ir["tabulate_tensor"]
        code = "code generated from %s" % tt
        return code

class ufc_cell_integral(ufc_integral):
    def __init__(self):
        ufc_integral.__init__(self, "cell")

class ufc_exterior_facet_integral(ufc_integral):
    def __init__(self):
        ufc_integral.__init__(self, "exterior_facet")

class ufc_interior_facet_integral(ufc_integral):
    def __init__(self):
        ufc_integral.__init__(self, "interior_facet")

class ufc_custom_integral(ufc_integral):
    def __init__(self):
        ufc_integral.__init__(self, "custom")

    def num_cells(self, L, ir):
        value = ir["num_cells"]
        return L.Return(L.LiteralInt(value))

class ufc_vertex_integral(ufc_integral):
    def __init__(self):
        ufc_integral.__init__(self, "vertex")


### Code generation utilities:

def flat_array(L, name, dims):
    return L.FlattenedArray(L.Symbol(name), dims=dims)


### Inline math expressions:

def det_22(B, i, j, k, l):
    return B[i, k]*B[j, l] - B[i, l]*B[j, k]

def codet_nn(A, rows, cols):
    n = len(rows)
    if n == 2:
        return det_22(A, rows[0], rows[1], cols[0], cols[1])
    else:
        r = rows[0]
        subrows = rows[1:]
        parts = []
        for i, c in enumerate(cols):
            subcols = cols[i+1:] + cols[:i]
            parts.append(A[r, c] * codet_nn(A, subrows, subcols))
        return sum(parts[1:], parts[0])

def det_nn(A, n):
    if n == 1:
        return A[0, 0]
    else:
        ns = list(range(n))
        return codet_nn(A, ns, ns)

def __pdet_m1(L, A, m):
    # Special case 1xm for simpler expression
    i = L.Symbol("i")
    A2 = A[i,0]*A[i,0] # TODO: Translate to code
    return L.Call("sqrt", A2)

def __pdet_23(L, A):
    # Special case 2x3 for simpler expression
    i = L.Symbol("i")

    # TODO: Translate to code:
    c = cross_expr(A[:,0], A[:,1])
    c2 = c[i]*c[i]

    return L.Call("sqrt", c2)

def pdet_mn(A, m, n):
    """Compute the pseudo-determinant of A: sqrt(det(A.T*A))."""
    # TODO: This would be more usable if it didn't make up variable names...
    # Build A^T*A matrix
    i = L.Symbol("i")
    j = L.Symbol("j")
    k = L.Symbol("k")
    ATA = L.ArrayDecl("double", "ATA", shape=(n, n), values=0)
    body = L.AssignAdd(ATA[i, j], A[k, i] * A[k, j])
    body = L.ForRange(k, 0, m, body=body)
    body = L.ForRange(j, 0, n, body=body)
    body = L.ForRange(i, 0, n, body=body)

    # Take determinant and square root
    return L.Call("sqrt", det_nn(ATA, n))

def pdet_expr(A, m, n):
    """Compute the pseudo-determinant of A: sqrt(det(A.T*A))."""
    if n == 1:
        return pdet_mn(A, m, n)
        #return pdet_m1(A, m)
    elif m == 3 and n == 2:
        return pdet_mn(A, m, n)
        #return pdet_32(A)
    else:
        return pdet_mn(A, m, n)

def det_expr(A, m, n):
    "Compute the (pseudo-)determinant of A."
    if m == n:
        return det_nn(A, m)
    else:
        return pdet_expr(A, m, n)


class ufc_domain(ufc_generator):
    def __init__(self):
        ufc_generator.__init__(self, "", domain_implementation)

    def cell_shape(self, L, ir):
        name = ir["cell_shape"]
        return L.Return(L.Symbol(name))

    def topological_dimension(self, L, ir):
        "Default implementation of returning topological dimension fetched from ir."
        value = ir["topological_dimension"]
        return L.Return(L.LiteralInt(value))

    def geometric_dimension(self, L, ir):
        "Default implementation of returning geometric dimension fetched from ir."
        value = ir["geometric_dimension"]
        return L.Return(L.LiteralInt(value))

    def create_coordinate_finite_element(self, L, ir):
        classname = ir["create_coordinate_finite_element"] # FIXME: ffc passes class id not name
        return L.Return(L.New(classname))

    def create_coordinate_dofmap(self, L, ir):
        classname = ir["create_coordinate_dofmap"] # FIXME: ffc passes class id not name
        return L.Return(L.New(classname))

    def compute_physical_coordinates(self, L, ir):
        # Dimensions
        gdim = ir["geometric_dimension"]
        tdim = ir["topological_dimension"]
        num_points = L.Symbol("num_points")

        # Loop indices
        ip = L.Symbol("ip")
        i = L.Symbol("i")
        j = L.Symbol("j")

        # Input cell data
        coordinate_dofs = L.Symbol("coordinate_dofs")
        cell_orientation = L.Symbol("cell_orientation")

        # Output geometry
        x = flat_array(L, "x", (num_points, gdim))[ip]

        # Input geometry
        X = flat_array(L, "X", (num_points, tdim))[ip]

        # Assign to x[ip][i]
        body = L.Assign(x[i], 0) # FIXME: Almost exactly like jacobian implementation, solve issues there first

        # Carry out for each component i
        body = L.ForRange(i, 0, gdim, body=body)

        # Carry out for all points
        return L.ForRange(ip, 0, num_points, body=body)

    def compute_reference_coordinates(self, L, ir):
        # Dimensions
        gdim = ir["geometric_dimension"]
        tdim = ir["topological_dimension"]
        num_points = L.Symbol("num_points")

        # Loop indices
        ip = L.Symbol("ip")
        i = L.Symbol("i")
        j = L.Symbol("j")

        # Input cell data
        coordinate_dofs = L.Symbol("coordinate_dofs")
        cell_orientation = L.Symbol("cell_orientation")

        # Output geometry
        X = flat_array(L, "X", (num_points, tdim))[ip]

        # Input geometry
        x = flat_array(L, "x", (num_points, gdim))[ip]

        # Assign to X[j]
        body = L.Assign(X[j], 0) # FIXME: Newton loop to invert x(X)

        # Carry out for each component j
        body = L.ForRange(j, 0, tdim, body=body)

        # Carry out for all points
        return L.ForRange(ip, 0, num_points, body=body)

    def compute_jacobians(self, L, ir):
        # FIXME: Get data for scalar coordinate subelement:
        num_scalar_dofs = 3 # ir["num_scalar_coordinate_element_dofs"]
        scalar_coordinate_element_classname = "fixmecec" # ir["scalar_coordinate_element_classname"]

        # Dimensions
        gdim = ir["geometric_dimension"]
        tdim = ir["topological_dimension"]
        num_points = L.Symbol("num_points")

        # Loop indices
        ip = L.Symbol("ip")
        i = L.Symbol("i")
        j = L.Symbol("j")
        d = L.Symbol("d")

        # Input cell data
        coordinate_dofs = flat_array(L, "coordinate_dofs", (num_scalar_dofs, gdim)) # FIXME: Correct block structure of dofs?
        cell_orientation = L.Symbol("cell_orientation") # need this?

        # Output geometry
        J = flat_array(L, "J", (num_points, gdim, tdim))

        # Input geometry
        X = flat_array(L, "X", (num_points, tdim))

        # Declare basis derivatives table
        dphi = L.Symbol("dphi")
        dphi_dims = (tdim, num_scalar_dofs) # FIXME: Array layout to match eval_ref_bas_deriv
        dphi_decl = L.ArrayDecl("double", dphi, sizes=dphi_dims)

        # Computing table one point at a time instead of using
        # num_points will allow skipping dynamic allocation
        one_point = 1

        # Define scalar finite element instance (stateless, so placing this on the stack is free)
        # FIXME: To do this we'll need to #include the element header. Find a solution with dijitso!!
        #        When that's fixed, we have a solution for custom integrals as well.
        define_element = "%s element;" % (scalar_coordinate_element_classname,)
        func = "element.evaluate_reference_basis_derivatives" # FIXME: Use correct function to compute basis derivatives here

        # Compute basis derivatives table
        compute_dphi = L.Call(func, (dphi, one_point, L.AddressOf(X[ip, 0]))) # FIXME: eval_ref_bas_deriv signature

        # Make table more accessible with dimensions
        dphi = L.FlattenedArray(dphi, dims=dphi_dims)

        # Assign to J[ip][i][j] for each component i,j
        J_loop = L.AssignAdd(J[ip, i, j], coordinate_dofs[d, i]*dphi[j, d]) # FIXME: Array layout of dphi to match eval_ref_bas_deriv
        J_loop = L.ForRange(d, 0, num_scalar_dofs, body=J_loop)
        J_loop = L.ForRange(j, 0, tdim, body=J_loop)
        J_loop = L.ForRange(i, 0, gdim, body=J_loop)

        # Carry out computation of dphi and J accumulation for each point
        point_body = L.StatementList([compute_dphi, J_loop])
        point_loop = L.ForRange(ip, 0, num_points, body=point_body)

        body = L.StatementList([define_element, dphi_decl, point_loop])
        return body


    def compute_jacobian_determinants(self, L, ir):
        # Dimensions
        gdim = ir["geometric_dimension"]
        tdim = ir["topological_dimension"]
        num_points = L.Symbol("num_points")

        # Loop indices
        ip = L.Symbol("ip")
        i = L.Symbol("i")
        j = L.Symbol("j")

        # Output geometry
        detJ = L.Symbol("detJ")[ip]

        # Input geometry
        J = flat_array(L, "J", (num_points, gdim, tdim))[ip]

        # Assign to detJ
        body = L.Assign(detJ, det_expr(J, gdim, tdim)) # TODO: Call Eigen instead?

        # Carry out for all points
        loop = L.ForRange(ip, 0, num_points, body=body)

        code = loop #[defines, loop]
        return code

    def compute_jacobian_inverses(self, L, ir):
        # Dimensions
        gdim = ir["geometric_dimension"]
        tdim = ir["topological_dimension"]
        num_points = L.Symbol("num_points")

        # Loop indices
        ip = L.Symbol("ip")
        i = L.Symbol("i")
        j = L.Symbol("j")

        # Input cell data
        coordinate_dofs = L.Symbol("coordinate_dofs")
        cell_orientation = L.Symbol("cell_orientation")

        # Output geometry
        K = flat_array(L, "K", (num_points, tdim, gdim))[ip]

        # Input geometry
        J = flat_array(L, "J", (num_points, gdim, tdim))[ip]
        detJ = L.Symbol("detJ")[ip]

        # Assign to K[j][i] for each component j,i
        body = L.Assign(K[j][i], 0.0) # FIXME: Call Eigen?
        body = L.ForRange(i, 0, gdim, body=body)
        body = L.ForRange(j, 0, tdim, body=body)

        # Carry out for all points
        return L.ForRange(ip, 0, num_points, body=body)

    def compute_geometry(self, L, ir):
        # Output geometry
        x = L.Symbol("x")
        J = L.Symbol("J")
        detJ = L.Symbol("detJ")
        K = L.Symbol("K")

        # Dimensions
        num_points = L.Symbol("num_points")

        # Input geometry
        X = L.Symbol("X")

        # Input cell data
        coordinate_dofs = L.Symbol("coordinate_dofs")
        cell_orientation = L.Symbol("cell_orientation")

        # All arguments
        args = (x, J, detJ, K, num_points, X, coordinate_dofs, cell_orientation)

        # Just chain calls to other functions here
        code = [
            L.Call("compute_physical_coordinates", (x, num_points, X, coordinate_dofs, cell_orientation)),
            L.Call("compute_jacobians", (J, num_points, X, coordinate_dofs, cell_orientation)),
            L.Call("compute_jacobian_determinants", (detJ, num_points, J)),
            L.Call("compute_jacobian_inverses", (K, num_points, J, detJ)),
            ]
        return L.StatementList(code)
