__author__ = "Anders Logg (logg@tti-c.org)"
__date__ = "2004-09-27"
__copyright__ = "Copyright (c) 2004 Anders Logg"
__license__  = "GNU GPL Version 2"

# Python modules
from Numeric import *

# FFC modules
import dolfin
import latex
from rank import Rank
from index import *
from algebra import *
from reassign import *
from integrator import Integrator
from finiteelement import FiniteElement

class Form:

    """A Form represents a multi-linear form typically appearing in
    the variational formulation of partial differential equation.

    A Form holds the following data:

        sum   - the representation of the form as a Sum
        ranks - a list of auxiliary data for each Product"""

    def __init__(self, form):
        "Create Form."

        if isinstance(form, Form):
            self.sum = reassign_indices(Sum(form.sum))
            self.ranks = [Rank(p) for p in self.sum.products]
        else:
            self.sum = reassign_indices(Sum(form))
            self.ranks = [Rank(p) for p in self.sum.products]

        # Check that all Products have the same primary rank,
        # otherwise it's not a multi-linear form.
        for i in range(len(self.ranks) - 1):
            if not self.ranks[i].r0 == self.ranks[i + 1].r0:
                raise RuntimeError, "Form must be linear in each of its arguments."

        print "Created form: " + str(self)
        
        return

    def compile(self, language = "C++"):
        "Generate code for evaluation of the variational form."

        # Compute the reference tensor for each product
        for i in range(len(self.sum.products)):
            A0 = self.compute_reference_tensor(i)
            print "A0 = " + str(A0)

        # Choose language
        if language == "C++":
            dolfin.compile(A0)
        elif language == "LaTeX":
            latex.compile(A0)
        else:
            print "Unknown language " + str(language)
        return

    def compute_reference_tensor(self, i):
        "Compute the integrals of the reference tensor."

        product = self.sum.products[i]
        r0 = self.ranks[i].r0
        r1 = self.ranks[i].r1
        dims = self.ranks[i].dims
        imap = self.ranks[i].imap

        # Create dimensions for tensor indices (appearing in a Factor).
        # Note that dims contains the dimensions for all indices, even
        # indices that are not part of the tensor (but which are present
        # in the geometry tensor and should be contracted away). We thus
        # need to extract the dimensions only for the indices which are
        # part of the tensor.
        tensordims = [dims[i] for i in imap]

        # Create reference tensor and a list of all indices
        A0 = zeros(tensordims, Float)
        tensorindices = build_indices(tensordims)

        # Create quadrature rule
        integrate = Integrator(product)

        # Iterate over all combinations of indices
        index = zeros(len(dims))
        for tensorindex in tensorindices:

            # Update indices. Note that we need to supply values for
            # all indices, not only the tensor indices.
            for i in range(len(tensorindex)):
                index[imap[i]] = tensorindex[i]

            # Compute the integral
            A0[tensorindex] = integrate(product, index, r0, r1)
            
        return A0

    def __repr__(self):
        "Print nicely formatted representation of Form."
        output = "a("
        r0 = self.ranks[0].r0 # All primary ranks are equal
        for i in range(r0):
            if i < (r0 - 1):
                output += "v" + str(i) + ", "
            else:
                output += "v" + str(i) + ") = "
        output += self.sum.__repr__()
        return output

if __name__ == "__main__":

    print "Testing form compiler"
    print "---------------------"

    element = FiniteElement("Lagrange", 1, "triangle")
    
    u = BasisFunction(element)
    v = BasisFunction(element)
    i = Index()
    
    a = Form(u.dx(i)*v.dx(i) + u*v)
    a.compile()
