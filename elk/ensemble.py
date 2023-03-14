import numpy as np
import lightkurve as lk
import matplotlib.pyplot as plt
import scipy
from astropy.table import Table, Column, join, vstack
from astropy.io import fits
import astropy.units as u

import os.path
import gc

from .lightcurve import SimpleCorrectedLightcurve, TESSCutLightcurve


class EnsembleLC:
    def __init__(self, radius, cluster_age, output_path="./", cluster_name=None, location=None,
                 percentile=80, cutout_size=99, scattered_light_frequency=5, n_pca=6, verbose=False,
                 no_lk_cache=False, debug=False):
        """Class for generating lightcurves from TESS cutouts

        Parameters
        ----------
        radius : `float`
            Radius of the cluster. If a `float` is given then unit is assumed to be degrees. Otherwise, I'll
            convert your unit to what I need.
        cluster_age : `float`
            Age of the cluster. If a `float` is given then unit is assumed to be dex. Otherwise, I'll
            convert your unit to what I need.
        output_path : `str`, optional
            Path to a folder in which to save outputs - must have subfolders Corrected_LCs/ and Figures/LCs/,
            by default "./"
        cluster_name : `str`, optional
            Name of the cluster, by default None
        location : `str`, optional
            Location of the cluster #TODO What format here?, by default None
        percentile : `int`, optional
            Which percentile to use in the upper limit calculation, by default 80
        cutout_size : `int`, optional
            How large to make the cutout, by default 99
        scattered_light_frequency : `int`, optional
            Frequency at which to check for scattered light, by default 5
        n_pca : `int`, optional
            Number of principle components to use in the DesignMatrix, by default 6
        verbose : `bool`, optional
            Whether to print out information and progress bars, by default False
        no_lk_cache : `bool`, optional
            Whether to skip using the LightKurve cache and scrub downloads instead (can be useful for runs
            on a computing cluster with limited memory space), by default False
        debug : `bool`, optional
            #TODO DELETE THIS, by default False
        """

        # make sure that some sort of identifier has been provided
        assert cluster_name is not None or location is not None,\
            "Must provide at least one of `cluster_name` and `location`"

        # convert radius to degrees if it has units
        if hasattr(radius, 'unit'):
            radius = radius.to(u.deg).value

        # convert cluster age to dex if it has units
        if hasattr(cluster_age, 'unit'):
            if cluster_age.unit == u.dex:
                cluster_age = cluster_age.value
            else:
                cluster_age = np.log10(cluster_age.to(u.yr).value)

        # check main output folder
        if output_path is not None and not os.path.exists(output_path):
            print(f"WARNING: There is no output folder at the path that you supplied ({output_path})")
            create_it = input(("  Would you like me to create it for you? "
                               "(If not then no files will be saved) [Y/n]"))
            if create_it == "" or create_it.lower() == "y":
                # create the folder
                os.mkdir(output_path)
            else:
                output_path = None

        # if we wan't to avoid the lk cache we shall need our own dummy
        if no_lk_cache and not os.path.exists(os.path.join(output_path, 'cache')):
            os.mkdir(os.path.join(output_path, 'cache'))
            os.mkdir(os.path.join(output_path, 'cache', 'tesscut'))

        # check subfolders
        self.save = {"lcs": False, "figures": False}
        if output_path is not None:
            for subpath, key in zip(["Corrected_LCs", os.path.join("Figures", "LCs")], ["lcs", "figures"]):
                path = os.path.join(output_path, subpath)
                if not os.path.exists(path):
                    print(f"WARNING: The necessary subfolder at ({path}) does not exist")
                    create_it = input(("  Would you like me to create it for you? "
                                       "(If not then these files will not be saved) [Y/n]"))
                    if create_it == "" or create_it.lower() == "y":
                        # create the folder
                        os.makedirs(path)
                        self.save[key] = True
                else:
                    self.save[key] = True

        self.lcs = []
        self.output_path = output_path
        self.radius = radius
        self.cluster_age = cluster_age
        self.callable = cluster_name if cluster_name is not None else location
        self.cluster_name = cluster_name
        self.location = location
        self.percentile = percentile
        self.cutout_size = cutout_size
        self.scattered_light_frequency = scattered_light_frequency
        self.n_pca = n_pca
        self.verbose = verbose
        self.no_lk_cache = no_lk_cache
        self.debug = debug

        # We are also going to document how many observations failed each one of our quality tests
        self.n_failed_download = 0
        self.n_near_edge = 0
        self.n_scattered_light = 0
        self.n_good_obs = 0

    def __repr__(self):
        return f"<{self.__class__.__name__} - {self.callable}>"

    def previously_downloaded(self):
        """Check whether the files have previously been downloaded for this cluster

        Returns
        -------
        exists : `bool`
            Whether the file exists
        """
        path = os.path.join(self.output_path, 'Corrected_LCs/', str(self.callable) + 'output_table.fits')
        return os.path.exists(path)

    def has_tess_data(self):
        """Check whether TESS has data on the cluster

        Returns
        -------
        has_data : `bool`
            Whether these is at least one observation in TESS
        """
        # search for the cluster in TESS using lightkurve
        self.tess_search_results = lk.search_tesscut(self.callable)
        self.sectors_available = len(self.tess_search_results)
        if self.verbose:
            print(f'{self.callable} has {self.sectors_available} observations')
        return self.sectors_available > 0

    def downloadable(self, ind):
        # use a Try statement to see if we can download the cluster data
        try:
            download_dir = os.path.join(self.output_path, 'cache') if self.no_lk_cache else None
            tpfs = self.tess_search_results[ind].download(cutout_size=(self.cutout_size, self.cutout_size),
                                                          download_dir=download_dir)
        except lk.search.SearchError:
            tpfs = None
        return tpfs

    def clear_cache(self):
        """Clear the folder containing manually cached lightkurve files"""
        for file in os.listdir(os.path.join(self.output_path, 'cache', 'tesscut')):
            if file.endswith(".fits"):
                os.remove(os.path.join(self.output_path, 'cache', 'tesscut', file))

    def scattered_light(self, quality_tpfs, full_model_Normalized):
        if self.debug:
            return False
        # regular grid covering the domain of the data
        X, Y = np.meshgrid(np.arange(0, self.cutout_size, 1), np.arange(0, self.cutout_size, 1))
        XX = X.flatten()
        YY = Y.flatten()

        # Define the steps for which we test for scattered light
        time_steps = np.arange(0, len(quality_tpfs), self.scattered_light_frequency)
        coefficients_array = np.zeros((len(time_steps), 3))
        data_flux_values = (quality_tpfs - full_model_Normalized).flux.value

        for i in range(len(time_steps)):
            data = data_flux_values[time_steps[i]]
            # best-fit linear plane
            A = np.c_[XX, YY, np.ones(XX.shape)]
            C, _, _, _ = scipy.linalg.lstsq(A, data.flatten())    # coefficients
            coefficients_array[i] = C

            # Deleting defined items we don't need any more to save memory
            del A
            del C
            del data

        X_cos = coefficients_array[:, 0]
        Y_cos = coefficients_array[:, 1]
        Z_cos = coefficients_array[:, 2]

        mxc = max(abs(X_cos))
        myc = max(abs(Y_cos))
        mzc = max(abs(Z_cos))

        #Deleting defined items we don't need any more to save memory
        del X_cos
        del Y_cos
        del Z_cos
        del coefficients_array
        gc.collect() #This is a command which will delete stray arguments to save memory

        return (mzc > 2.5) | ((mxc > 0.02) & (myc > 0.02))

    def get_lcs(self):
        """Get lightcurves for each of the observations of the cluster

        Returns
        -------
        good_obs : `int`
            Number of good observations
        sectors_available : `int`
            How many sectors of data are available
        which_sectors_good : :class:`~numpy.ndarray`
            Which sectors are good
        failed_download : `int`
            How many observations failed to download
        near_edge_or_Sector_1 : `int`
            The number of sectors where the target is located too close to the edge of a TESS detector, 
            where we cannot accurately perform uniform background subtraction; or the custer was observed in
            Sector 1, which has known systematics
        scattered_light : `int`
            The number of sectors with significant scattered light after the correction process. 'Significant'
            is arbitrarily defined by a by-eye calibration, and the threshold values can be changed if needed
        lc_Lens : :class:`~numpy.ndarray`
            The length of each lightcurve
        """
        self.lcs = [None for _ in range(self.sectors_available)]

        # start iterating through the observations
        for sector_ind in range(self.sectors_available):
            if self.verbose:
                print(f"Starting Quality Tests for Observation: {sector_ind}")

            # if we are avoiding caching then delete every fits file in the cache folder
            if self.no_lk_cache:
                self.clear_cache()

            # First is the Download Test
            tpfs = self.downloadable(sector_ind)
            if (tpfs is None) & (sector_ind + 1 < self.sectors_available):
                if self.verbose:
                    print('Failed Download')
                self.n_failed_download += 1
                continue
            elif (tpfs is None) & (sector_ind + 1 == self.sectors_available):
                if self.verbose:
                    print('Failed Download')
                self.n_failed_download += 1
                return

            lc = TESSCutLightcurve(tpfs=tpfs, radius=self.radius, cutout_size=self.cutout_size,
                                   percentile=self.percentile, n_pca=self.n_pca, progress_bar=self.verbose)

            # Now Edge Test
            near_edge = lc.near_edge()
            if near_edge & (sector_ind + 1 < self.sectors_available):
                if self.verbose:
                    print('Failed Near Edge Test')
                self.n_near_edge += 1
                continue
            if near_edge & (sector_ind + 1 == self.sectors_available):
                if self.verbose:
                    print('Failed Near Edge Test')
                self.n_near_edge += 1
                return

            lc.correct_lc()

            scattered_light_test = self.scattered_light(lc.quality_tpfs, lc.full_model_normalized)
            if scattered_light_test & (sector_ind + 1 < self.sectors_available):
                if self.verbose:
                    print("Failed Scattered Light Test")
                self.n_scattered_light += 1
                continue
            if scattered_light_test & (sector_ind + 1 == self.sectors_available):
                if self.verbose:
                    print("Failed Scattered Light Test")
                self.n_scattered_light += 1
                return
            else:
                # This Else Statement means that the Lightcurve is good and has passed our quality checks
                if self.verbose:
                    print(sector_ind, "Passed Quality Tests")
                self.n_good_obs += 1
                self.lcs[sector_ind] = lc

                # Now I am going to save a plot of the light curve to go visually inspect later
                range_ = max(lc.corrected_lc.flux.value) - min(lc.corrected_lc.flux.value)
                fig = plt.figure()
                plt.title(f'Observation: {sector_ind}')
                plt.plot(lc.corrected_lc.time.value, lc.corrected_lc.flux.value, color='k', linewidth=.5)
                plt.xlabel('Delta Time [Days]')
                plt.ylabel('Flux [e/s]')
                plt.text(lc.corrected_lc.time.value[0], (max(lc.corrected_lc.flux.value)-(range_*0.05)),
                         self.callable, fontsize=14)

                if self.output_path is not None and self.save["figures"]:
                    path = os.path.join(self.output_path, "Figures", "LCs",
                                        f'{self.callable}_Full_Corrected_LC_Observation_{sector_ind}.png')
                    plt.savefig(path, format='png', bbox_inches="tight")
                plt.close(fig)

        if self.no_lk_cache():
            self.clear_cache()

    def lightcurves_summary_file(self):
        """Generate lightcurve output files for the cluster and save them in `self.output_path`

        Returns
        -------
        output_table : :class:`~astropy.table.Table`
            The full lightcurves output table that was saved
        """
        LC_PATH = os.path.join(self.output_path, 'Corrected_LCs/',
                               str(self.callable) + 'output_table.fits')
        # Test to see if I have already downloaded and corrected this cluster, If I have, read in the data
        if self.previously_downloaded():
            output_table = Table.read(LC_PATH, hdu=1)
            return output_table

        if self.has_tess_data():
            # This section refers to the Cluster Not Previously Being Downloaded
            # So Calling function to download and correct data
            self.get_lcs()

            # clear out the cache after we're done making lightcurves
            if self.no_lk_cache:
                self.clear_cache()

        hdr = fits.Header()
        hdr['Name'] = self.cluster_name
        hdr['Location'] = self.location
        hdr["Radius [deg]"] = self.radius
        hdr['Log Age'] = self.cluster_age
        hdr["Has_TESS_Data"] = self.sectors_available > 0
        hdr["n_obs_available"] = self.sectors_available
        hdr["n_good_obs"] = self.n_good_obs
        hdr["n_failed_download"] = self.n_failed_download
        hdr["n_near_edge"] = self.n_near_edge
        hdr["n_scattered_light"] = self.n_scattered_light
        empty_primary = fits.PrimaryHDU(header=hdr)
        hdul = fits.HDUList([empty_primary] + [lc.hdu for lc in self.lcs if lc is not None])
        if self.output_path is not None:
            hdul.writeto(LC_PATH)

    def access_lightcurve(self, observation):
        """Function to access downloaded and corrected sector lightcurved 

        Parameters
        ----------
        observation : 'int'
            This is the number of the observation you wish to access the lightcurve for. 
            For example, if there were 4 observations available and 3 good observations, and you wish to 
            access the 2nd good observation, you would set observation to 2.

        Returns
        -------
        figure, table
            This will return a figure of the lightcurve for the given observation, as well as the light curve
            in table form.
        """
        path = os.path.join(self.output_path, 'Corrected_LCs', self.callable + 'output_table.fits')
        if not os.path.exists(path):
            print(("WARNING: The Lightcurve has not been downloaded/corrected. "
                   "Please run 'lightcurves_summary_file()' function for this cluster."))
            return None, None
        output_table = Table.read(path, hdu=1)

        # Get the Light Curve
        if output_table['n_good_obs'] == 1:
            light_curve_table = Table.read(path, hdu=2)
        else:
            light_curve_table = Table.read(path, hdu=(int(observation)+2))

        # Now I am going to save a plot of the light curve to go visually inspect later
        range_ = max(light_curve_table['flux']) - min(light_curve_table['flux'])
        fig = plt.figure()
        plt.title(f'Observation: {observation}')
        plt.plot(light_curve_table['time'], light_curve_table['flux'], color='k', linewidth=.5)
        plt.xlabel('Delta Time [Days]')
        plt.ylabel('Flux [e/s]')
        plt.text(light_curve_table['time'][0], (max(light_curve_table['flux'])-(range_*0.05)),
                 self.cluster_name, fontsize=14)

        plt.show()
        plt.close(fig)

        return fig, light_curve_table


def from_fits(filepath, **kwargs):
    new_ecl = EnsembleLC(cluster_name="", radius=None, cluster_age=None, output_path=None, **kwargs)
    with fits.open(filepath) as hdul:
        details = hdul[0]
        new_ecl.cluster_name = details.header["Name"]
        new_ecl.location = details.header["Location"]
        new_ecl.callable = new_ecl.cluster_name if new_ecl.cluster_name is not None else new_ecl.location
        new_ecl.radius = details.header["Radius [deg]"]
        new_ecl.cluster_age = details.header["Log_Age"]
        new_ecl.sectors_available = details.header["n_obs_available"]
        new_ecl.n_good_obs = details.header["n_good_obs"]
        new_ecl.n_failed_download = details.header["n_failed_download"]
        new_ecl.n_near_edge = details.header["n_near_edge"]
        new_ecl.n_scattered_light = details.header["n_scattered_light"]

        new_ecl.lcs = [SimpleCorrectedLightcurve(fits_path=filepath, hdu_index=hdu_ind)
                       for hdu_ind in range(1, len(hdul))]

    return new_ecl
