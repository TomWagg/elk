import numpy as np

__all__ = ["flux_to_mag", "flux_err_to_mag_err"]

""" ANSI color codes """
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
END = "\033[0m"


def flux_to_mag(flux):
    """Converts flux to TESS Magnitudes

    Parameters
    ----------
    flux : :class:`~numpy.ndarray` or `float`
        flux in terms of ergs/s 

    Returns
    -------
    mag : :class:`~numpy.ndarray` or `float`
        TESS Magnitude centered in the I band
    """
    m1 = 10     # calibration mag
    f1 = 15000  # Calibration Flux corresponding to the calibration mag
    mag = 2.5 * np.log10(f1 / flux) + m1
    return mag


def flux_err_to_mag_err(flux, flux_err):
    """Converts TESS Flux Errors to Magnitude errors

    Parameters
    ----------
    flux : class: `~numpy.ndarray` or `float`
        flux in terms of ergs/s
    flux_err : class: `~numpy.ndarray` or `float`
       flux error in terms of ergs/s

    Returns
    -------
   mag_err : class: `~numpy.ndarray` or `float` 
        TESS Magnittude errors 
    """
    d_mag_d_flux = -2.5 / (flux * np.log(10))
    m_err_squared = abs(d_mag_d_flux)**2 * flux_err**2
    return np.sqrt(m_err_squared)


def print_warning(*args):
    """Print a warning in yellow, prefixed by WARNING"""
    print(f"{YELLOW}WARNING:{END}", *args)


def print_failure(*args):
    """Print something in red"""
    print(RED, end="")
    print(*args, END)


def print_success(*args):
    """Print something in green"""
    print(GREEN, end="")
    print(*args, END)
