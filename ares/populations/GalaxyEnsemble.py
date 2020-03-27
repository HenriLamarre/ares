"""

GalaxyEnsemble.py

Author: Jordan Mirocha
Affiliation: UCLA
Created on: Wed Jul  6 11:02:24 PDT 2016

Description: 

"""

import os
import gc
import time
import pickle
import numpy as np
from ..util import read_lit
from ..util.Math import smooth
from ..util import ProgressBar
from ..util.Survey import Survey
from .Halo import HaloPopulation
from scipy.optimize import curve_fit
from .GalaxyCohort import GalaxyCohort
from scipy.interpolate import interp1d
from scipy.integrate import quad, cumtrapz
from ..util.Photometry import what_filters
from ..analysis.BlobFactory import BlobFactory
from ..util.Stats import bin_e2c, bin_c2e, bin_samples
from ..static.SpectralSynthesis import SpectralSynthesis
from ..sources.SynthesisModelSBS import SynthesisModelSBS
from ..physics.Constants import rhodot_cgs, s_per_yr, s_per_myr, \
    g_per_msun, c, Lsun, cm_per_kpc, cm_per_pc, cm_per_mpc, E_LL, E_LyA, \
    erg_per_ev

try:
    import h5py
except ImportError:
    pass
    
tiny_MAR = 1e-30    
           
_linfunc = lambda x, p0, p1: p0 * (x - 8.) + p1
_cubfunc = lambda x, p0, p1, p2: p0 * (x - 8.)**2 + p1 * (x - 8.) + p2       
       
pars_affect_mars = ["pop_MAR", "pop_MAR_interp", "pop_MAR_corr"]
pars_affect_sfhs = ["pop_scatter_sfr", "pop_scatter_sfe", "pop_scatter_mar"]
pars_affect_sfhs.extend(["pop_update_dt", "pop_thin_hist"])

class GalaxyEnsemble(HaloPopulation,BlobFactory):
    
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        # May not actually need this...
        HaloPopulation.__init__(self, **kwargs)
        
    def __dict__(self, name):
        if name in self.__dict__:
            return self.__dict__[name]
        
        raise NotImplemented('help!')
        
    #@property
    #def dust(self):
    #    if not hasattr(self, '_dust'):
    #        self._dust = DustCorrection(**self.pf)
    #    return self._dust    
    
    @property
    def tab_z(self):
        if not hasattr(self, '_tab_z'):
            h = self._gen_halo_histories()
        return self._tab_z    
            
    @tab_z.setter
    def tab_z(self, value):
        self._tab_z = value
    
    @property
    def tab_t(self):
        if not hasattr(self, '_tab_t'):
            # Array of times corresponding to all z' > z [years]
            self._tab_t = self.cosm.t_of_z(self.tab_z) / s_per_yr    
        return self._tab_t
        
    @property
    def tab_dz(self):
        if not hasattr(self, '_tab_dz'):
            dz = np.diff(self.tab_z)
            if np.allclose(np.diff(dz), 0):
                dz = dz[0]
            self._tab_dz = dz    
                
        return self._tab_dz
        
    @property
    def _b14(self):
        if not hasattr(self, '_b14_'):
            self._b14_ = read_lit('bouwens2014')
        return self._b14_
        
    @property
    def _c94(self):
        if not hasattr(self, '_c94_'):
            self._c94_ = read_lit('calzetti1994').windows
        return self._c94_
                
    @property
    def _nircam(self):
        if not hasattr(self, '_nircam_'):
            nircam = Survey(cam='nircam')
            nircam_M = nircam._read_nircam(filter_set='M')
            nircam_W = nircam._read_nircam(filter_set='W')
            
            self._nircam_ = nircam_M, nircam_W
        return self._nircam_    
            
    def run(self):
        return
        
    def cSFRD(self, z, Mh):
        """
        Compute cumulative SFRD as a function of lower-mass bound.
        """
        
        if type(Mh) not in [list, np.ndarray]:
            Mh = np.array([Mh])
        
        iz = np.argmin(np.abs(z - self.histories['z']))
        _Mh = self.histories['Mh'][:,iz]
        _sfr = self.histories['SFR'][:,iz]
        _w = self.histories['nh'][:,iz]
        
        # Really this is the number of galaxies that formed in a given
        # differential redshift slice.
        SFRD = np.zeros_like(Mh)
        for i, _mass in enumerate(Mh):
            ok = _Mh >= _mass
            SFRD[i] = np.sum(_sfr[ok==1] * _w[ok==1]) / rhodot_cgs
        
        SFRD_tot = self.SFRD(z)    
            
        return SFRD / SFRD_tot
        
    def SFRD(self, z, Mmin=None):
        """
        Will convert to internal cgs units.
        """
                
        if type(z) in [int, float, np.float64]:
        
            iz = np.argmin(np.abs(z - self.histories['z']))
            sfr = self.histories['SFR'][:,iz]
            w = self.histories['nh'][:,iz]
            
            if Mmin is not None:
                _Mh = self.histories['Mh'][:,iz]
                ok = _Mh >= Mmin
            else:
                ok = np.ones_like(sfr)
                
            ok = sfr > 0    
                        
            # Really this is the number of galaxies that formed in a given
            # differential redshift slice.
            return np.sum(sfr[ok==1] * w[ok==1]) / rhodot_cgs
        else:
            sfrd = np.zeros_like(z)
            for k, _z in enumerate(z):
                
                iz = np.argmin(np.abs(_z - self.histories['z']))
                _sfr = self.histories['SFR'][:,iz]
                _w = self.histories['nh'][:,iz]
                
                if Mmin is not None:
                    _Mh = self.histories['Mh'][:,iz]
                    ok = _Mh >= Mmin
                else:
                    ok = np.ones_like(_sfr)
                            
                ok = _sfr > 0
                            
                sfrd[k] = np.sum(_sfr[ok==1] * _w[ok==1]) / rhodot_cgs
                
            return sfrd    
        #return np.trapz(sfr[0:-1] * dw, dx=np.diff(Mh)) / rhodot_cgs
        
    def _sfrd_func(self, z):
        # This is a cheat so that the SFRD spline isn't constructed
        # until CALLED. Used only for tunneling (see `pop_tunnel` parameter). 
        return self.SFRD(z)    

    def tile(self, arr, thin, renorm=False):
        """
        Expand an array to `thin` times its size. Group elements such that
        neighboring bundles of `thin` elements are objects that formed
        at the same redshift.
        """
        if arr is None:
            return None
        
        if thin in [0, 1]:
            return arr.copy()

        assert thin % 1 == 0

        # First dimension: formation redshifts
        # Second dimension: observed redshifts / times        
        #new = np.tile(arr, (int(thin), 1))
        new = np.repeat(arr, int(thin), axis=0)
        #N = arr.shape[0]
        #_new = np.tile(arr, int(thin) * N)
        #new = _new.reshape(N * int(thin), N)
        
        if renorm:
            return new / float(thin)
        else:
            return new
    
    def noise_normal(self, arr, sigma):
        noise = np.random.normal(scale=sigma, size=arr.size)
        return np.reshape(noise, arr.shape)
    
    def noise_lognormal(self, arr, sigma):
        lognoise = np.random.normal(scale=sigma, size=arr.size)        
        #noise = 10**(np.log10(arr) + np.reshape(lognoise, arr.shape)) - arr
        noise = np.power(10, np.log10(arr) + np.reshape(lognoise, arr.shape)) \
              - arr
        return noise
            
    @property    
    def tab_scatter_mar(self):
        if not hasattr(self, '_tab_scatter_mar'):            
            self._tab_scatter_mar = np.random.normal(scale=sigma, 
                size=np.product(self.tab_shape))
        return self._tab_scatter_mar
    
    @tab_scatter_mar.setter
    def tab_scatter_mar(self, value):
        assert value.shape == self.tab_shape
        self._tab_scatter_mar = value
        
    @property
    def tab_shape(self):
        if not hasattr(self, '_tab_shape'):
            raise AttributeError('help')
        return self._tab_shape
    
    @tab_shape.setter
    def tab_shape(self, value):
        self._tab_shape = value
        
    @property
    def _cache_halos(self):
        if not hasattr(self, '_cache_halos_'):
            self._cache_halos_ = self._gen_halo_histories()
        return self._cache_halos_
        
    @_cache_halos.setter
    def _cache_halos(self, value):
        self._cache_halos_ = value
            
    def _gen_halo_histories(self):
        """
        From a set of smooth halo assembly histories, build a bigger set
        of histories by thinning, and (optionally) adding scatter to the MAR. 
        """            
        
        if hasattr(self, '_cache_halos_'):
            return self._cache_halos

        raw = self.load()
        
        thin = self.pf['pop_thin_hist']
        
        if thin > 1:
            assert self.pf['pop_histories'] is None, \
                "Set pop_thin_hist=pop_scatter_mar=0 if supplying pop_histories by hand."

        sigma_mar = self.pf['pop_scatter_mar']
        sigma_env = self.pf['pop_scatter_env']
                                        
        # Just read in histories in this case.
        if raw is None:
            print('Running halo trajectories...')
            zall, raw = self.guide.Trajectories()
            print('Done with halo trajectories.')
        else:
            zall = raw['z']
            
        # Should be in ascending redshift.
        assert np.all(np.diff(zall) > 0)

        nh_raw = raw['nh']
        Mh_raw = raw['Mh']
                        
        # May have to generate MAR if these are simulated halos
        if ('MAR' not in raw) and ('MAR_tot' not in raw):
            
            assert thin < 2
            assert sigma_mar == sigma_env == 0
            
            print("Generating MARs from Mh trajectories...")
            dM = -1. * np.diff(Mh_raw, axis=1)
            
            t = self.cosm.t_of_z(zall) * 1e6 / s_per_myr
            
            dt = -1. * np.diff(t) # in yr already    
            
            MAR_z = dM / dt

            zeros = np.ones((Mh_raw.shape[0], 1)) * tiny_MAR
            # Follow ARES convention of forward differencing, so must pad MAR
            # array with zeros at the lowest redshift snapshot.
            mar_raw = np.hstack((zeros, MAR_z))
            
        else:
            if 'MAR' in raw:
                mar_raw = raw['MAR']
            elif self.pf['pop_mergers']:
                mar_raw = raw['MAR_acc']
            else:
                mar_raw = raw['MAR_tot']
                
        ##
        # Throw away halos with Mh < Mmin or Mh > Mmax
        ##
        if self.pf['pop_synth_minimal'] and (self.pf['pop_histories'] is None):
                        
            Mmin = self.guide.Mmin(zall)
                        
            # Find boundary between halos that never cross Mmin and those
            # that do.
            is_viable = Mh_raw > Mmin[:,None]
                                     
            any_viable = np.sum(is_viable, axis=1)
            
            # Cut out halos that never exist in our mass range of interest.
            ilo = np.min(np.argwhere(any_viable > 0))
            ihi = np.max(np.argwhere(any_viable > 0)) + 1

            # Also cut out some redshift range.        
            zok = np.logical_and(zall >= self.pf['pop_synth_zmin'],
                zall <= self.pf['pop_synth_zmax'])
            zall = zall[zok==1]
            
            # Modify our arrays            
            Mh_raw = Mh_raw[ilo:ihi,zok==1]
            nh_raw = nh_raw[ilo:ihi,zok==1]
            mar_raw = mar_raw[ilo:ihi,zok==1]
        
        ##
        # Could optionally thin out the bins to allow more diversity.
        if thin > 0:
            # Doesn't really make sense to do this unless we're
            # adding some sort of stochastic effects.
        
            # Remember: first dimension is the SFH identity.
            nh = self.tile(nh_raw, thin, True)
            Mh = self.tile(Mh_raw, thin)
        else:
            nh = nh_raw#.copy()
            Mh = Mh_raw#.copy()

        self.tab_shape = Mh.shape

        ##
        # Allow scatter in things
        ##            

        # Two potential kinds of scatter in MAR    
        mar = self.tile(mar_raw, thin)
        if sigma_env > 0:
            mar *= (1. + self.noise_normal(mar, sigma_env))

        if sigma_mar > 0:
            np.random.seed(self.pf['pop_scatter_mar_seed'])
            noise = self.noise_lognormal(mar, sigma_mar)
            mar += noise
            # Normalize by mean of log-normal to preserve mean MAR?
            mar /= np.exp(0.5 * sigma_mar**2)
            del noise

        # SFR = (zform, time (but really redshift))
        # So, halo identity is wrapped up in axis=0
        # In Cohort, formation time defines initial mass and trajectory (in full)
        #z2d = np.array([zall] * nh.shape[0])
        histories = {'Mh': Mh, 'MAR': mar, 'nh': nh}
        
        # Add in formation redshifts to match shape (useful after thinning)
        histories['zthin'] = self.tile(zall, thin)

        histories['z'] = zall
        
        if self.pf['conserve_memory']:
            dtype = np.float32
        else:
            dtype = np.float64
        
        t = np.array([self.cosm.t_of_z(zall[_i]) for _i in range(zall.size)]) \
            / s_per_myr
        
        histories['t'] = t.astype(dtype)
                            
        if self.pf['pop_dust_yield'] is not None:
            r = np.reshape(np.random.rand(Mh.size), Mh.shape)
            if self.pf['conserve_memory']:
                histories['rand'] = r.astype(np.float32)
            else:
                histories['rand'] = r
        else:
            pass
            
        if 'SFR' in raw:
            #assert sigma_mar == sigma_env == 0
            histories['SFR'] = raw['SFR']

        if 'Z' in raw:
            histories['Z'] = raw['Z']
            
        if 'children' in raw:
            histories['children'] = raw['children']
            
        if 'pos' in raw:
            histories['pos'] = raw['pos']

        self.tab_z = zall
        #self._cache_halos = histories
        
        del raw
        gc.collect()

        return histories
        
    def get_timestamps(self, zform):
        """
        For a halo forming at z=zform, return series of time and redshift
        steps based on halo dynamical time or other.
        """
        
        t0 = self.cosm.t_of_z(zform) / s_per_yr
        tf = self.cosm.t_of_z(0) / s_per_yr
        
        if self.pf['pop_update_dt'] == 'dynamical':
            t = t0
            steps_t = [t0]           # time since Big Bang
            steps_z = [zform]
            while t < tf:
                z = self.cosm.z_of_t(t * s_per_yr)
                _tdyn = self.halos.DynamicalTime(z, 1e10) / s_per_yr
            
                if t + _tdyn > tf:
                    steps_t.append(self.cosm.t_of_z(0.) / s_per_yr)
                    steps_z.append(0.)
                    break
            
                steps_t.append(t+_tdyn)
                steps_z.append(self.cosm.z_of_t((t+_tdyn) * s_per_yr))
                t += _tdyn
                
            steps_t = np.array(steps_t)
            steps_z = np.array(steps_z)
                
        else:
            dt = self.pf['pop_update_dt'] * 1e6     
            steps_t = np.arange(t0, tf+dt, dt)
            
            # Correct last timestep to make sure final timestamp == tf
            if steps_t[-1] > tf:
                steps_t[-1] = tf
            
            steps_z = np.array(map(self.cosm.z_of_t, steps_t * s_per_yr))
            
        return steps_t, steps_z
        
    @property
    def histories(self):
        if not hasattr(self, '_histories'):
            self._histories = self.RunSAM()
        return self._histories
        
    @histories.setter
    def histories(self, value):
        
        assert type(value) is dict
                
        must_flip = False
        if 'z' in value:
            if np.all(np.diff(value['z']) > 0):
                must_flip = True
            
        if must_flip:
            for key in value:
                if not type(value[key]) == np.ndarray:
                    continue
                    
                if value[key].ndim == 1:
                    value[key] = value[key][-1::-1]
                else:    
                    value[key] = value[key][:,-1::-1]
                    
        self._histories = value
        
    def Trajectories(self):
        return self.RunSAM()
    
    def RunSAM(self):
        """
        Run models. If deterministic, will just return pre-determined
        histories. Otherwise, will do some time integration.
        """
                
        return self._gen_galaxy_histories()
    
    @property
    def guide(self):
        if not hasattr(self, '_guide'):
            if self.pf['pop_guide_pop'] is not None:
                self._guide = self.pf['pop_guide_pop']
            else:
                tmp = self.pf.copy()
                tmp['pop_ssp'] = False
                self._guide = GalaxyCohort(**tmp)
        
        return self._guide
         
    def cmf(self, M):
        # Allow ParameterizedQuantity here
        pass
            
    @property
    def tab_cmf(self):
        if not hasattr(self, '_tab_cmf'):
            pass

    @property
    def _norm(self):
        if not hasattr(self, '_norm_'):
            mf = lambda logM: self.ClusterMF(10**logM)
            self._norm_ = quad(lambda logM: mf(logM) * 10**logM, -3, 10., 
                limit=500)[0]
        return self._norm_
        
    def ClusterMF(self, M, beta=-2, Mmin=50.):
        return (M / Mmin)**beta * np.exp(-Mmin / M)
        
    @property
    def tab_Mcl(self):
        if not hasattr(self, '_tab_Mcl'):
            self._tab_Mcl = np.logspace(-1., 8, 10000)
        return self._tab_Mcl
        
    @tab_Mcl.setter
    def tab_Mcl(self, value):
        self._tab_Mcl = value
    
    @property
    def tab_cdf(self):
        if not hasattr(self, '_tab_cdf'):
            mf = lambda logM: self.ClusterMF(10**logM)
            f_cdf = lambda M: quad(lambda logM: mf(logM) * 10**logM, -3, np.log10(M), 
                limit=500)[0] / self._norm
            self._tab_cdf = np.array(map(f_cdf, self.tab_Mcl))
            
        return self._tab_cdf    
    
    @tab_cdf.setter
    def tab_cdf(self, value):
        assert len(value) == len(self.tab_Mcl)
        self._tab_cdf = value
            
    def ClusterCDF(self):
        if not hasattr(self, '_cdf_cl'):            
            self._cdf_cl = lambda MM: np.interp(MM, self.tab_Mcl, self.tab_cdf)
        
        return self._cdf_cl
        
    @property
    def Mcl(self):
        if not hasattr(self, '_Mcl'):
            mf = lambda logM: self.ClusterMF(10**logM)
            self._Mcl = quad(lambda logM: mf(logM) * (10**logM)**2, -3, 10., 
                limit=500)[0] / self._norm
                
        return self._Mcl
        
    @property
    def tab_imf_me(self):
        if not hasattr(self, '_tab_imf_me'):
            self._tab_imf_me = 10**bin_c2e(self.src.pf['source_imf_bins'])
        return self._tab_imf_me
    
    @property
    def tab_imf_mc(self):
        if not hasattr(self, '_tab_imf_mc'):
            self._tab_imf_mc = 10**self.src.pf['source_imf_bins']
        return self._tab_imf_mc
        
    def _cache_ehat(self, key):
        if not hasattr(self, '_cache_ehat_'):
            self._cache_ehat_ = {}
            
        if key in self._cache_ehat_:
            return self._cache_ehat_[key]    
        
        return None    
        
    def _TabulateEmissivity(self, E=None, Emin=None, Emax=None, wave=None):
        """
        Compute emissivity over a grid of redshifts and setup interpolant.
        """   
        
        dz = self.pf['pop_synth_dz']
        zarr = np.arange(self.pf['pop_synth_zmin'], 
            self.pf['pop_synth_zmax'] + dz, dz)
        
        if (Emin is not None) and (Emax is not None):
            band = (Emin, Emax)
        else:
            band = None
        
        if (band is not None) and (E is not None):
            raise ValueError("You're being confusing! Supply `E` OR `Emin` and `Emax`")
            
        if wave is not None:
            raise NotImplemented('careful')    
                    
        hist = self.histories
        
        tab = np.zeros_like(zarr)
        for i, z in enumerate(zarr):
            
            # This will be [erg/s]
            L = self.synth.Luminosity(sfh=hist['SFR'], zobs=z, band=band,
                zarr=hist['z'], extras=self.extras)
            
            # OK, we've got a whole population here.
            nh = self.get_field(z, 'nh')
            Mh = self.get_field(z, 'Mh')
            
            # Modify by fesc
            if band is not None:
                if band[0] in [13.6, E_LL]:
                    # Doesn't matter what Emax is
                    fesc = self.guide.fesc(z=z, Mh=Mh)
                elif band in [(10.2, 13.6), (E_LyA, E_LL)]:
                    fesc = self.guide.fesc_LW(z=z, Mh=Mh)
                else:
                    fesc = 1.  
            else:
                fesc = 1.          
            
            # Integrate over halo population.
            tab[i] = np.sum(L * fesc * nh)

        return zarr, tab / cm_per_mpc**3
        
    def Emissivity(self, z, E=None, Emin=None, Emax=None):
        """
        Compute the emissivity of this population as a function of redshift
        and rest-frame photon energy [eV].

        Parameters
        ----------
        z : int, float

        Returns
        -------
        Emissivity in units of erg / s / c-cm**3 [/ eV]

        """

        on = self.on(z)
        if not np.any(on):
            return z * on

        # Need to build an interpolation table first.
        # Cache also by E, Emin, Emax
        
        cached_result = self._cache_ehat((E, Emin, Emax))
        if cached_result is not None:
            func = cached_result
        else:
            
            zarr, tab = self._TabulateEmissivity(E, Emin, Emax)
            
            tab[np.logical_or(tab <= 0, np.isinf(tab))] = 1e-70
            
            func = interp1d(zarr, np.log10(tab), kind='cubic', 
                bounds_error=False, fill_value=-np.inf)
            
            self._cache_ehat_[(E, Emin, Emax)] = func#zarr, tab
                     
        return 10**func(z)
        #return self._cache_ehat_[(E, Emin, Emax)](z)
        
    def PhotonLuminosityDensity(self, z, E=None, Emin=None, Emax=None):
        # erg / s / cm**3
        rhoL = self.Emissivity(z, E=E, Emin=Emin, Emax=Emax)
        erg_per_phot = self._get_energy_per_photon(Emin, Emax) * erg_per_ev
                                                              
        return rhoL / np.mean(erg_per_phot)

    def _gen_stars(self, idnum, Mh):
        """
        Take draws from cluster mass function until stopping criterion met.
        
        Return the amount of mass formed in this burst.
        """
        
        z = self._arr_z[idnum]
        t = self._arr_t[idnum]
        dt = (self._arr_t[idnum+1] - self._arr_t[idnum]) # in Myr
        
        E_h = self.halos.BindingEnergy(z, Mh)
        
        # Statistical approach from here on out.
        Ms = 0.0
        
        Mg = self._arr_Mg_c[idnum] * 1.
        
        # Number of supernovae from stars formed previously
        N_SN_p = self._arr_SN[idnum] * 1.
        E_UV_p = self._arr_UV[idnum] * 1.
        # Number of supernovae from stars formed in this timestep.
        N_SN_0 = 0. 
        E_UV_0 = 0.
        
        N_MS_now = 0
        m_edg = self.tab_imf_me
        m_cen = self.tab_imf_mc
        imf = np.zeros_like(m_cen)
        
        fstar_gmc = self.pf['pop_fstar_cloud']
        
        delay_fb_sne = self.pf['pop_delay_sne_feedback']
        delay_fb_rad = self.pf['pop_delay_rad_feedback']
                            
        vesc = self.halos.EscapeVelocity(z, Mh) # cm/s
                        
        # Form clusters until we use all the gas or blow it all out.    
        ct = 0
        Mw = 0.0
        Mw_rad = 0.0
        while (Mw + Mw_rad + Ms) < Mg * fstar_gmc:
            
            r = np.random.rand()
            Mc = np.interp(r, self.tab_cdf, self.tab_Mcl)
            
            # If proposed cluster would take up all the rest of our 
            # gas (and then some), don't let it happen.
            if (Ms + Mc + Mw) >= Mg:
                break
            
            ##
            # First things first. Figure out the IMF in this cluster.
            ##
                
            # Means we're doing cheap spectral synthesis.
            if self.pf['pop_sample_imf']:
                # For now, just scale UV luminosity with Nsn?
                
                # Uniform IMF placeholder
                #r2 = 0.1 + np.random.rand(1000) * (200. - 0.1)
                r2 = self._stars.draw_stars(1000000)

                # Integrate until we get Mc. 
                m2 = np.cumsum(r2)

                # What if, by chance, first star drawn is more massive than
                # Mc?
                
                cut = np.argmin(np.abs(m2 - Mc)) + 1
             
                if cut >= len(r2):
                    cut = len(r2) - 1
                    #print(r2.size, Mc, m2[-1] / Mc)
                    #raise ValueError('help')
                
                hist, edges = np.histogram(r2[0:cut+1], bins=m_edg)
                imf += hist
                                
                N_MS = np.sum(hist[m_cen >= 8.])
                
                # Ages of the stars
                
                #print((Mw + Ms) / Mg, Nsn, Mc / 1e3)
                                                                
            else:
                # Expected number of SNe if we were forming lots of clusters.
                lam = Mc * self._stars.nsn_per_m
                N_MS = np.random.poisson(lam)
                hist = 0.0
                
            ##
            # Now, take stars and let them feedback on gas.
            ##    

            ## 
            # Increment stuff
            ##      
            Ms += Mc
            imf += hist
            
            # Move along.
            if N_MS == 0:
                ct += 1
                continue
            
            ## 
            # Can delay feedback or inject it instantaneously.
            ##
            if delay_fb_sne == 0:
                N_SN_0 += N_MS  
            elif delay_fb_sne == 1:
                ##
                # SNe all happen at average delay time from formation.
                ##
                
                if self.pf['pop_sample_imf']:
                    raise NotImplemented('help')

                # In Myr
                avg_delay = self._stars.avg_sn_delay
                
                # Inject right now if timestep is long.
                if dt > avg_delay:
                    N_SN_0 += N_MS     
                else:
                    # Figure out when these guys will blow up.
                    #N_SN_next += N_MS
                    
                    tnow = self._arr_t[idnum]
                    tfut = self._arr_t[idnum:]
                                            
                    iSNe = np.argmin(np.abs((tfut - tnow) - avg_delay))
                    #if self._arr_t[idnum+iSNe] < avg_delay:
                    #    iSNe += 1
                    
                    print('hey 2', self._arr_t[idnum+iSNe] - tnow)
                    
                    self._arr_SN[idnum+iSNe] += N_MS                    
                                        
            elif delay_fb_sne == 2:
                ##
                # Actually spread SNe out over time according to DTD.
                ##
                delays = self._stars.draw_delays(N_MS)
                
                tnow = self._arr_t[idnum]
                tfut = self._arr_t[idnum:]
                
                # Could be more precise since closest index may be
                # slightly shorter than delay time.
                iSNe = np.array([np.argmin(np.abs((tfut - tnow) - delay)) \
                    for delay in delays])
                
                # Add SNe one by one.
                for _iSNe in iSNe:
                    self._arr_SN[idnum+_iSNe] += 1    
                
                # Must make some of the SNe happen NOW!
                N_SN_0 += sum(iSNe == 0)
                
            ##
            # Make a wind
            ##        
            Mw = 2 * (N_SN_0 + N_SN_p) * 1e51 * self.pf['pop_coupling_sne'] \
               / vesc**2 / g_per_msun
               
            ##                                 
            # Stabilize with some radiative feedback?
            ##
            if not self.pf['pop_feedback_rad']:
                ct += 1
                continue
                 
            ##
            # Dump in UV from 'average' massive star.
            ##
            if self.pf['pop_delay_rad_feedback'] == 0:
            
                # Mask out low-mass stuff? Only because scaling by N_MS
                massive = self._stars.Ms >= 8.
                
                LUV = self._stars.tab_LUV
                  
                Lavg = np.trapz(LUV[massive==1] * self._stars.tab_imf[massive==1], 
                    x=self._stars.Ms[massive==1]) \
                     / np.trapz(self._stars.tab_imf[massive==1], 
                    x=self._stars.Ms[massive==1])
                   
                life = self._stars.tab_life 
                tavg = np.trapz(life[massive==1] * self._stars.tab_imf[massive==1], 
                    x=self._stars.Ms[massive==1]) \
                     / np.trapz(self._stars.tab_imf[massive==1], 
                    x=self._stars.Ms[massive==1])    
                    
                corr = np.minimum(tavg / dt, 1.)
                                    
                E_UV_0 += Lavg * N_MS * dt * corr * 1e6 * s_per_yr
                                
                #print(z, 2 * E_rad / vesc**2 / g_per_msun / Mw)
                
                Mw_rad += 2 * (E_UV_0 + E_UV_p) * self.pf['pop_coupling_rad'] \
                    / vesc**2 / g_per_msun
            
            elif self.pf['pop_delay_rad_feedback'] >= 1:
                raise NotImplemented('help')
                
                delays = self._stars.draw_delays(N_MS)
                
                tnow = self._arr_t[idnum]
                tfut = self._arr_t[idnum:]
                
                # Could be more precise since closest index may be
                # slightly shorter than delay time.
                iSNe = np.array([np.argmin(np.abs((tfut - tnow) - delay)) \
                    for delay in delays])
                
                # Add SNe one by one.
                for _iSNe in iSNe:
                    self._arr_SN[idnum+_iSNe] += 1
                
            # Count clusters    
            ct += 1    
            
        ##
        # Back outside loop over clusters.
        ##    
                                    
        return Ms, Mw, imf
        
    def deposit_in(self, tnow, delay):
        """
        Determin index of time-array in which to deposit some gas, energy,
        etc., given current time and requested delay.
        """
        
        inow = np.argmin(np.abs(tnow - self._arr_t))
        
        tfut = self._arr_t[inow:]
                                
        ifut = np.argmin(np.abs((tfut - tnow) - delay))
                
        return inow + ifut
                
    def _gen_galaxy_history(self, halo, zobs=0):
        """
        Evolve a single galaxy in time. 
        
        Parameters
        ----------
        halo : dict
            Contains growth history of the halo of interest in order of
            *ascending redshift*. Must contain (at least) 'z', 't', 'Mh',
            and 'nh' keys.
        
        """
        
        # Grab key pieces of info
        z = halo['z'][-1::-1]
        t = halo['t'][-1::-1]
        Mh_s = halo['Mh'][-1::-1]
        MAR = halo['MAR'][-1::-1]
        nh = halo['nh'][-1::-1]
        
        self._arr_t = t
        self._arr_z = z
        
        zeros_like_t = np.zeros_like(t)
        
        self._arr_SN = zeros_like_t.copy()
        self._arr_UV = zeros_like_t.copy()
        self._arr_Mg_c = zeros_like_t.copy()
        self._arr_Mg_t = zeros_like_t.copy()
        
        # Short-hand
        fb = self.cosm.fbar_over_fcdm
        Mg_s = fb * Mh_s
        Nt = len(t)
        
        assert np.all(np.diff(t) >= 0)

        zform = max(z[Mh_s>0])

        SFR = np.zeros_like(Mh_s)
        Ms  = np.zeros_like(Mh_s)
        Msc = np.zeros_like(Mh_s)
        #Mg_t  = np.zeros_like(Mh_s)
        #Mg_c = np.zeros_like(Mh_s)
        Mw  = np.zeros_like(Mh_s)
        E_SN  = np.zeros_like(Mh_s)
        Nsn  = np.zeros_like(Mh_s)
        L1600 = np.zeros_like(Mh_s)
        bursty = np.zeros_like(Mh_s)
        #imf = np.zeros((Mh_s.size, self.tab_imf_mc.size))
        #fc_r = np.ones_like(Mh_s)
        #fc_i = np.ones_like(Mh_s)
                        
        ok = Mh_s > 0                
                        
        # Generate smooth histories 'cuz sometimes we need that.
        MAR_s = np.array([self.guide.MAR(z=z[k], Mh=Mh_s[k]).squeeze() \
            for k in range(Nt)])
        SFE_s = np.array([self.guide.SFE(z=z[k], Mh=Mh_s[k]) \
            for k in range(Nt)])
        SFR_s = fb * SFE_s * MAR_s
        
        # Some characteristic timescales...
        tdyn = self.halos.DynamicalTime(z) / s_per_myr
        
        # in Myr
        delay_fb = self.pf['pop_delay_sne_feedback']
        
        
        ###
        ## THIS SHOULD ONLY HAPPEN WHEN NOT DETERMINISTIC
        ###
        ct = 0
        for i, _Mh in enumerate(Mh_s):

            if _Mh == 0:
                continue

            if z[i] < zobs:
                break
                
            if i == Nt - 1:
                break

            # In years    
            dt = (t[i+1] - t[i]) * 1e6

            if z[i] == zform:
                self._arr_Mg_t[i] = fb * _Mh
                
            # Determine gas supply
            if self.pf['pop_multiphase']:
                ifut = self.deposit_in(t[i], tdyn[i])
                self._arr_Mg_c[ifut] = self._arr_Mg_t[i] * 1
            else:
                self._arr_Mg_c[i] = self._arr_Mg_t[i] * 1
                    
            E_h = self.halos.BindingEnergy(z[i], _Mh)
            
            ##
            # Override switch to smooth inflow-driven star formation model.s
            ##
            if E_h > (1e51 * self.pf['pop_force_equilibrium']):
                vesc = self.halos.EscapeVelocity(z[i], _Mh)
                NSN_per_M = self._stars.nsn_per_m
                
                # Assume 1e51 * SNR * dt = 1e51 * SFR * SN/Mstell * dt = E_h
                eta = 2. * self.pf['pop_coupling_sne'] * 1e51 * NSN_per_M \
                    / g_per_msun / vesc**2
                                
                # SFR = E_h / 1e51 / (SN/Ms) / dt
                SFR[i]  = fb * MAR[i] / (1. + eta)
                Ms[i+1] = 0.5 * (SFR[i] + SFR[i-1]) * dt
                Mg[i+1] = Mg[i] + Macc - Ms[i+1]
                continue
                        
            ##
            # FORM STARS!
            ##
            
            # Gas we will accrete on this timestep
            Macc = fb * 0.5 * (MAR[i+1] + MAR[i]) * dt
            
            # What fraction of gas is in a phase amenable to star formation?
            if self.pf['pop_multiphase']:
                ifut = self.deposit_in(t[i], tdyn[i])
                self._arr_Mg_c[ifut] += Macc
            else:
                # New gas available to me right away in this model.
                self._arr_Mg_c[i] += Macc
                
            ##
            # Here we go.
            ##    
            _Mnew, _Mw, _imf = self._gen_stars(i, _Mh)    

            self._arr_Mg_t[i+1] = \
                max(self._arr_Mg_t[i] + Macc - _Mnew - _Mw, 0.)
 
            # Deal with cold gas.    
            if self.pf['pop_multiphase']:    
                #pass
                # Add remaining cold gas to reservoir for next timestep?
                # Keep gas hot for longer?
                # Subtract wind from cold gas reservoir?
                # Question is, do we feedback on gas that's already hot,
                # or gas that was "on deck" to form stars?
                
                #ifut = self.deposit_in(t[i], tdyn[i])
                #
                #self._arr_Mg_c[i:ifut] -= _Mw / (float(ifut - i))
                
                if self._arr_Mg_c[i+1] < 0:
                    print("Correcting for negative mass.", z[i])
                    self._arr_Mg_c[i+1] = 0
                
            #else:    
            #    self._arr_Mg_c[i+1] = self._arr_Mg_t[i+1]
            
            # Flag this step as bursty.
            bursty[i] = 1
                            
            # Save SFR. Set Ms, Mg for next iteration.
            SFR[i]   = _Mnew / dt
            imf[i]   = _imf
            Ms[i+1]  = _Mnew # just the stellar mass *formed this step*
            Msc[i+1] = Msc[i] + _Mnew
            
            ct += 1
            
        
        keep = np.ones_like(z)#np.logical_and(z > zobs, z <= zform)
        
        data = \
        { 
         'SFR': SFR[keep==1],
         'MAR': MAR_s[keep==1],
         'Mg': self._arr_Mg_t[keep==1], 
         'Mg_c':self._arr_Mg_c[keep==1], 
         'Ms': Msc[keep==1], # *cumulative* stellar mass!
         'Mh': Mh_s[keep==1], 
         'nh': nh[keep==1],
         'Nsn': Nsn[keep==1],
         'bursty': bursty[keep==1],
         #'imf': imf[keep==1],
         'z': z[keep==1],
         't': t[keep==1],
         'zthin': halo['zthin'][-1::-1],
        }
        
        if 'rand' in halo:
            data['rand'] = halo['rand'][-1::-1]
        
                
        return data
            
    def _gen_galaxy_histories(self, zstop=0):     
        """
        Take halo histories and paint on galaxy histories in some way.
        
        If pop_stochastic, must operate on each galaxy individually using
        `self._gen_galaxy_history`, otherwise, can 'evolve' galaxies
        deterministically all at once.
        """
                
        # First, grab halos
        halos = self._gen_halo_histories()
                                        
        ## 
        # Stochastic model
        ##
        if self.pf['pop_sample_cmf']:
            
            fields = ['SFR', 'MAR', 'Mg', 'Ms', 'Mh', 'nh', 
                'Nsn', 'bursty', 'rand']
            num = halos['Mh'].shape[0]
            
            hist = {key:np.zeros_like(halos['Mh']) for key in fields}
            
            # This guy is 3-D
            #hist['imf'] = np.zeros((halos['Mh'].size, halos['z'].size,
            #    self.tab#_imf_mc.size))
            
            for i in range(num):
                
                print(i)
                #halo = {key:halos[key][i] for key in keys}
                halo = {'t': halos['t'], 'z': halos['z'], 
                    'zthin': halos['zthin'], 'rand': halos['rand'],
                    'Mh': halos['Mh'][i], 'nh': halos['nh'][i],
                    'MAR': halos['MAR'][i]}
                data = self._gen_galaxy_history(halo, zstop)
                
                for key in fields:
                    hist[key][i] = data[key]
        
            hist['z'] = halos['z']
            hist['t'] = halos['t']
            hist['zthin'] = halos['zthin']
        
            flip = {key:hist[key][-1::-1] for key in hist.keys()}
        
            self.histories = flip
            return flip                                            
        
        
        ##
        # Simpler models. No need to loop over all objects individually.
        ##
        
        # Eventually generalize
        assert self.pf['pop_update_dt'].startswith('native')
        native_sampling = True

        Nhalos = halos['Mh'].shape[0]

        # Flip arrays to be in ascending time.
        z = halos['z'][-1::-1]
        z2d = z[None,:]
        t = halos['t'][-1::-1]
        Mh = halos['Mh'][:,-1::-1]
        nh = halos['nh'][:,-1::-1]
        
        # Will have been corrected for mergers in `load` if pop_mergers==1
        MAR = halos['MAR'][:,-1::-1]
                        
        # 't' is in Myr, convert to yr
        dt = np.abs(np.diff(t)) * 1e6
        dt_myr = dt / 1e6
                        
        ##
        # OK. We've got a bunch of halo histories and we need to integrate them
        # to get things like stellar mass, metal mass, etc. This means we need
        # to make an assumption about how halos grow between our grid points.
        # If we assume smooth histories, we should be trying to do a trapezoidal
        # integration in log-space. However, given that we often add noise to
        # MARs, in that case we should probably just assume flat MARs within
        # the timestep. 
        #
        # Note also that we'll need to zero-pad these arrays to keep their 
        # shapes the same after we integrate (just so we don't have indexing
        # problems later). Since we're doing a simple sum, we'll fill the 
        # elements corresponding to the lowest redshift bin with zeros since we
        # can't compute luminosities after that point (no next grid pt to diff
        # with).
        ##
        
        fb = self.cosm.fbar_over_fcdm
        fZy = self.pf['pop_mass_yield'] * self.pf['pop_metal_yield']
        
        if self.pf['pop_dust_yield'] is not None:
            fd = self.guide.dust_yield(z=z2d, Mh=Mh)
        else:
            fd = 0.0
        
        if self.pf['pop_dust_growth'] is not None:
            fg = self.guide.dust_growth(z=z2d, Mh=Mh)
        else:
            fg = 0.0
            
        fmr = self.pf['pop_mass_yield']    
        fml = (1. - fmr)

        # Integrate (crudely) mass accretion rates
        #_Mint = cumtrapz(_MAR[:,:], dx=dt, axis=1)
        #_MAR_c = 0.5 * (np.roll(MAR, -1, axis=1) + MAR)
        #_Mint = np.cumsum(_MAR_c[:,1:] * dt, axis=1)

        if 'SFR' in halos:
            SFR = halos['SFR'][:,-1::-1]
        else:      
            SFR = self.guide.SFE(z=z2d, Mh=Mh)
            np.multiply(SFR, MAR, out=SFR)
            SFR *= fb
            
        ##
        # Duty cycle effects
        ##
        if self.pf['pop_fduty'] is not None:
            
            np.random.seed(self.pf['pop_fduty_seed'])
            
            fduty = self.guide.fduty(z=z2d, Mh=Mh)
            T_on = self.pf['pop_fduty_dt']

            if T_on is not None:
                
                fduty_avg = np.mean(fduty, axis=1)
                
                # Create random bursts with length `T_on`
                dt_tot = t.max() - t.min() # Myr
                
                i_on = int(T_on / dt_myr[0])
                                
                # t is in ascending order                
                Nt = float(t.size)
                on = np.zeros_like(Mh, dtype=bool)
                
                for i in range(Nhalos):
                    
                    if np.all(SFR[i] == 0):
                        continue
                    
                    r = np.random.rand(t.size)
                    on_prop = r < fduty[i]
                                                    
                    if fduty_avg[i] == 1:
                        on[i,:] = True
                    else:                    
                        ct = 0
                        while (on[i].sum() / Nt) < fduty_avg[i]:
                            j = np.random.randint(low=0, high=int(Nt))
                            
                            on[i,j:j+i_on] = True
                        
                            ct += 1

                off = np.logical_not(on)
                
            else:
                # Random numbers for all mass and redshift points
                r = np.reshape(np.random.rand(Mh.size), Mh.shape)

                off = r >= fduty
            
            SFR[off==True] = 0

        # Never do this!
        if self.pf['conserve_memory']:
            raise NotImplemented('this is deprecated')
            dtype = np.float32
        else:
            dtype = np.float64
            
        zeros_like_Mh = np.zeros((Nhalos, 1), dtype=dtype)

        # Stellar mass should have zeros padded at the 0th time index
        Ms = np.hstack((zeros_like_Mh,
            np.cumsum(SFR[:,0:-1] * dt * fml, axis=1)))

        #Ms = np.zeros_like(Mh)

        if self.pf['pop_flag_sSFR'] is not None:
            sSFR = SFR / Ms

        if self.pf['pop_Mmin'] is not None:
            above_Mmin = Mh >= self.pf['pop_Mmin']
        else:
            Mmin = self.halos.VirialMass(z2d, self.pf['pop_Tmin'])
            above_Mmin = Mh >= Mmin
            
        # Bye bye guys
        SFR *= above_Mmin
            
        ##
        # Introduce some by-hand quenching.
        if self.pf['pop_quench'] is not None:
            zreion = self.pf['pop_quench']
            if type(zreion) in [np.ndarray, np.ma.core.MaskedArray]:
                assert zreion.size == Nhalos, \
                    "Supplied array of reionization redshifts is the wrong size!"
                    
                is_quenched = np.logical_and(z2d <= zreion[:,None], 
                    Mh < self.pf['pop_Mmin'])
                        
            else:    
                is_quenched = zreion(z=z2d, Mh=Mh)
                
            # Print some quenched fraction vs. redshift to help debug?
            if self.pf['debug']:
                k = np.argmin(np.abs(z - 10.))
                print('Quenched fraction at z=10:', 
                    np.sum(is_quenched[:,k]) / float(Nhalos))
                
                k = np.argmin(np.abs(z - 7.))
                print('Quenched fraction at z=7:', 
                    np.sum(is_quenched[:,k]) / float(Nhalos))
            
            # Bye bye guys
            SFR *= np.logical_not(is_quenched)
                                                
        ##          
        # Dust           
        ##
        if np.any(fd > 0):
                        
            delay = self.pf['pop_dust_yield_delay']

            if np.all(fg == 0):
                if type(fd) in [int, float, np.float64] and delay == 0:
                    Md = fd * fZy * Ms
                else:
                    
                    if delay > 0:
                        
                        assert np.allclose(np.diff(dt_myr), 0.0, 
                            rtol=1e-5, atol=1e-5)

                        shift = int(delay // dt_myr[0])
                        
                        # Need to fix so Mh-dep fd can still work.
                        assert type(fd) in [int, float, np.float64]
                        
                        DPR = np.roll(SFR, shift, axis=1)[:,0:-1] \
                            * dt * fZy * fd
                        DPR[:,0:shift] = 0.0
                    else:
                        DPR = SFR[:,0:-1] * dt * fZy * fd[:,0:-1]
                    
                    Md = np.hstack((zeros_like_Mh,
                        np.cumsum(DPR, axis=1)))
            else:       
                                
                # Handle case where growth in ISM is included.
                if type(fg) in [int, float, np.float64]:
                    fg = fg * np.ones_like(SFR)
                if type(fd) in [int, float, np.float64]:
                    fd = fd * np.ones_like(SFR)
                                    
                # fg^-1 is like a rate coefficient [has units yr^-1]
                Md = np.zeros_like(SFR)
                for k, _t in enumerate(t[0:-1]):
                    
                    # Dust production rate
                    Md_p = SFR[:,k] * fZy * fd[:,k]
                    
                    # Dust growth rate
                    Md_g = Md[:,k] / fg[:,k]
                    
                    Md[:,k+1] = Md[:,k] + (Md_p + Md_g) * dt[k]
                    
            # Dust surface density.
            Sd = Md / 4. / np.pi / self.guide.dust_scale(z=z2d, Mh=Mh)**2
                
            # Can add scatter to surface density 
            if self.pf['pop_dust_scatter'] is not None:
                sigma = self.guide.dust_scatter(z=z2d, Mh=Mh)
                noise = np.zeros_like(Sd)
                np.random.seed(self.pf['pop_dust_scatter_seed'])
                for _i, _z in enumerate(z):
                    noise[:,_i] = self.noise_lognormal(Sd[:,_i], sigma[:,_i])
                                
                Sd += noise
                
            # Convert to cgs. Do in two steps in case conserve_memory==True.
            Sd *= g_per_msun / cm_per_kpc**2
                             
            if self.pf['pop_dust_fcov'] is not None:
                fcov = self.guide.dust_fcov(z=z2d, Mh=Mh)
            else:
                fcov = 1.
                
        else:
            Md = Sd = 0.
            Rd = np.inf
            fcov = 1.0
            
        del z2d    
        
        # Metal mass
        if 'Z' in halos:
            Z = halos['Z']
            Mg = MZ = 0.0
        else:
            if self.pf['pop_enrichment']:
                MZ = Ms * fZy

                # Gas mass
                Mg = np.hstack((zeros_like_Mh, 
                    np.cumsum((MAR[:,0:-1] * fb - SFR[:,0:-1]) * dt, axis=1)))

                Z = MZ / Mg / self.pf['pop_fpoll']

                Z[Mg==0] = 1e-3
                Z = np.maximum(Z, 1e-3)

            else:
                MZ = Mg = Z = 0.0
                                
        ##
        # Merge halos, sum stellar masses, SFRs.
        # Only add masses after progenitors are absorbed.
        # Need to add luminosity from progenitor history even after merger.
        # NOTE: no transferrance of gas, metals, or stars, as of yet.
        ##        
        if self.pf['pop_mergers'] > 0:
            children = halos['children'][:,-1::-1]
            iz, iM = children.T
            uni = np.all(Mh.mask == False, axis=1)
            merged = np.logical_and(iz != -1, uni == True)
                                  
            pos = halos['pos'][:,-1::-1,:]
                                    
            for i in range(iz.size):
                if iz[i] == -1:
                    continue
                
                # May not be necessary
                if not merged[i]:
                    continue
                
                # Fill-in positions of merged parents                        
                #pos[i,0:iz[i],:] = pos[iM[i],iz[i],:]
                # Add SFR so luminosities include that of parent halos
                #SFR[iM[i],iz[i]:] += SFR[i,iz[i]:]
                
                # Assume merged mass has same stellar fraction as progenitor?
                
        # Limit to main branch        
        elif self.pf['pop_mergers'] == -1:
            children = halos['children'][:,-1::-1]
            iz, iM = children.T
            main_branch = iz == -1
            
            nh = nh[main_branch==1]
            Ms = Ms[main_branch==1]
            Mh = Mh[main_branch==1]
            #MAR = MAR[main_branch==1]
            #MZ = MZ[main_branch==1]
            Md = Md[main_branch==1]
            Sd = Sd[main_branch==1]
            #fcov = fcov[main_branch==1]
            #Mg = Mg[main_branch==1]
            #Z = Z[main_branch==1]
            SFR = SFR[main_branch==1]
            zeros_like_Mh = zeros_like_Mh[main_branch==1]
            
            if 'pos' in halos:
                pos = halos['pos'][main_branch==1,-1::-1,:]
            else:
                pos = None
        else:
            pos = None
            
        # Pack up                
        results = \
        {
         'nh': nh,
         'Mh': Mh,
         'MAR': MAR,  # May use this 
         't': t,
         'z': z,
         #'child': child,
         'zthin': halos['zthin'][-1::-1],
         #'z2d': z2d,
         'SFR': SFR,
         'Ms': Ms,
         'MZ': MZ,
         'Md': Md, 
         'Sd': Sd,
         'fcov': fcov,
         'Mh': Mh,
         'Mg': Mg,
         'Z': Z,
         'bursty': zeros_like_Mh,
         'pos': pos,
         #'imf': np.zeros((Mh.shape[0], self.tab_imf_mc.size)),
         'Nsn': zeros_like_Mh,
        }
                
        if self.pf['pop_dust_yield'] is not None:
            results['rand'] = halos['rand'][:,-1::-1]
            
        # Reset attribute!
        self.histories = results
                                
        return results
                
    def Slice(self, z, slc):
        """
        slice format = {'field': (lo, hi)}
        """
        
        iz = np.argmin(np.abs(z - self.tab_z))
        hist = self.histories
        
        c = np.ones(hist['Mh'].shape[0], dtype=int)
        for key in slc:
            lo, hi = slc[key]
            
            ok = np.logical_and(hist[key][:,iz] >= lo, hist[key][:,iz] <= hi)
            c *= ok
    
        # Build output
        to_return = {}        
        for key in self.histories:
            if self.histories[key].ndim == 1:
                to_return[key] = self.histories[key][c==1]
            else:    
                to_return[key] = self.histories[key][c==1,iz]
                
        return to_return

    def get_field(self, z, field):
        iz = np.argmin(np.abs(z - self.histories['z']))
        return self.histories[field][:,iz]
        
    def StellarMassFunction(self, z, bins=None, units='dex'):
        """
        Could do a cumulative sum to get all stellar masses in one pass. 
        
        For now, just do for the redshift requested.
        
        Parameters
        ----------
        z : int, float
            Redshift of interest.
        bins : array, float
            log10 stellar masses at which to evaluate SMF
        
        """
                                        
        cached_result = self._cache_smf(z, bins)
        if cached_result is not None:
            return cached_result
                            
        iz = np.argmin(np.abs(z - self.histories['z']))
        Ms = self.histories['Ms'][:,iz]
        nh = self.histories['nh'][:,iz]
         
        if (bins is None) or (type(bins) is not np.ndarray):
            binw = 0.5
            bin_c = np.arange(6., 13.+binw, binw)
        else:
            dx = np.diff(bins)
            assert np.allclose(np.diff(dx), 0)
            binw = dx[0]
            bin_c = bins
            
        bin_e = bin_c2e(bin_c)
        
        phi, _bins = np.histogram(Ms, bins=10**bin_e, weights=nh)
                
        if units == 'dex':
            # Convert to dex**-1 units
            phi /= binw
        else:
            raise NotImplemented('help')
                                
        self._cache_smf_[z] = bin_c, phi
                
        return self._cache_smf(z, bin_c)
        
    def XMHM(self, z, field='Ms', Mh=None, return_mean_only=False, Mbin=0.1):
        iz = np.argmin(np.abs(z - self.histories['z']))
                
        _Ms = self.histories[field][:,iz]
        _Mh = self.histories['Mh'][:,iz]
        logMh = np.log10(_Mh)
        
        fstar_raw = _Ms / _Mh
        
        if (Mh is None) or (type(Mh) is not np.ndarray):
            bin_c = np.arange(6., 14.+Mbin, Mbin)
        else:
            dx = np.diff(np.log10(Mh))
            assert np.allclose(np.diff(dx), 0)
            Mbin = dx[0]
            bin_c = np.log10(Mh)
            
        nh = self.get_field(z, 'nh')    
        x, y, z, N = bin_samples(logMh, np.log10(fstar_raw), bin_c, weights=nh)    

        if return_mean_only:
            return y

        return x, y, z
        
        
    def SMHM(self, z, Mh=None, return_mean_only=False, Mbin=0.1):
        """
        Compute stellar mass -- halo mass relation at given redshift `z`.
        
        .. note :: Because in general this is a scatter plot, this routine
            returns the mean and variance in stellar mass as a function of
            halo mass, the latter of which is defined via `Mh`.
        
        Parameters
        ----------
        z : int, float 
            Redshift of interest
        Mh : int, np.ndarray
            Halo mass bins (their centers) to use for histogram.
            Must be evenly spaced in log10
        
        """
        
        return self.XMHM(z, field='Ms', Mh=Mh, return_mean_only=return_mean_only,
            Mbin=Mbin)
        
    def find_trajectory(self, Mh, zh):
        l = np.argmin(np.abs(zh - self.tab_z))
        mass_at_zh = np.zeros_like(self.tab_z)
        for j, zform in enumerate(self.tab_z):
            if zform < zh:
                continue
                
            hist = self.histories['Mh'][j]
            
            mass_at_zh[j] = hist[l]
        
        # Find trajectory with mass closest to Mh at zh
        k = np.argmin(np.abs(Mh - mass_at_zh))
        
        return k
        
    @property
    def _stars(self):
        if not hasattr(self, '_stars_'):
            self._stars_ = SynthesisModelSBS(**self.src_kwargs)
        return self._stars_
        
    def _cache_L(self, key):
        if not hasattr(self, '_cache_L_'):
            self._cache_L_ = {}
            
        if key in self._cache_L_:
            return self._cache_L_[key]
        
        return None

    def _cache_lf(self, z, x=None, wave=None):
        if not hasattr(self, '_cache_lf_'):
            self._cache_lf_ = {}

        if (z, wave) in self._cache_lf_:            

            _x, _phi = self._cache_lf_[(z, wave)]
            
            # If no x supplied, return bin centers
            if x is None:
                return _x, _phi  
            
            if type(x) != np.ndarray:
                k = np.argmin(np.abs(x - _x))
                if abs(x - _x[k]) < 1e-3:
                    return _phi[k]
                else:
                    phi = 10**np.interp(x, _x, np.log10(_phi),
                        left=-np.inf, right=-np.inf)
                    
                    # If _phi is 0, interpolation will yield a NaN
                    if np.isnan(phi):
                        return 0.0
                    return phi
            if _x.size == x.size:
                if np.allclose(_x, x):
                    return _phi
            
            return 10**np.interp(x, _x, np.log10(_phi), 
                left=-np.inf, right=-np.inf)
                
        return None
        
    def _cache_smf(self, z, Ms):
        if not hasattr(self, '_cache_smf_'):
            self._cache_smf_ = {}
    
        if z in self._cache_smf_:
            _x, _phi = self._cache_smf_[z]    
                
            if Ms is None:
                return _phi        
            elif type(Ms) != np.ndarray:
                k = np.argmin(np.abs(Ms - _x))
                if abs(Ms - _x[k]) < 1e-3:
                    return _phi[k]
                else:
                    return 10**np.interp(Ms, _x, np.log10(_phi),
                        left=-np.inf, right=-np.inf)
            elif _x.size == Ms.size:
                if np.allclose(_x, Ms):
                    return _phi
            
            return 10**np.interp(Ms, _x, np.log10(_phi),
                left=-np.inf, right=-np.inf)
            
        return None  
        
    def get_history(self, i):
        """
        Extract a single halo's trajectory from full set, return in dict form.
        """
        
        # These are kept in ascending redshift just to make life difficult.
        raw = self.histories
                                
        hist = {'t': raw['t'], 'z': raw['z'], 
            'SFR': raw['SFR'][i], 'Mh': raw['Mh'][i], 
            'bursty': raw['bursty'][i], 'Nsn': raw['Nsn'][i]}
            
        if self.pf['pop_dust_yield'] is not None:
            hist['rand'] = raw['rand'][i]
            hist['Sd'] = raw['Sd'][i]
            
        if self.pf['pop_enrichment']:
            hist['Z'] = raw['Z'][i]
        
        if 'child' in raw:
            if raw['child'] is not None:
                hist['child'] = raw['child'][i]
            else:
                hist['child'] = None
        
        return hist
        
    def get_histories(self, z):
        for i in range(self.histories['Mh'].shape[0]):
            yield self.get_history(i)
            
    @property
    def synth(self):
        if not hasattr(self, '_synth'):
            self._synth = SpectralSynthesis(**self.pf)
            self._synth.src = self.src
            self._synth.oversampling_enabled = self.pf['pop_ssp_oversample']
            self._synth.oversampling_below = self.pf['pop_ssp_oversample_age']
            self._synth.careful_cache = self.pf['pop_synth_cache_level']
                        
        return self._synth
            
    def Magnitude(self, z, MUV=None, wave=1600., cam=None, filters=None, 
        filter_set=None, dlam=20., method='gmean', idnum=None, window=1,
        load=True, presets=None):
        """
        Return the absolution magnitude of objects at specified wavelength
        or as-estimated via given photometry.
        
        Parameters
        ----------
        z : int, float
            Redshift of object(s)
        wave : int, float
            If `cam` and `filters` aren't supplied, return the monochromatic
            AB magnitude at this wavelength [Angstroms].
        cam : str, tuple
            Single camera or tuple of cameras that contain the filters named
            in `filters`, e.g., cam=('wfc', 'wfc3')
        filters : tuple
            List (well, tuple) of filters to be used in estimating the
            magnitude of objects.
        method : str
            How to combined photometric measurements to estimate magnitude?
            Can compute geometric mean (method='gmean'), can interpolate
            between photometry to estimate magnitude at specified wavelength
            `wave` (method='interp'), use filter closest to wavelength provided
            (method='closest'), or return monochromatic magnitude (method='mono')
        dlam : int, float
            Wavelength resolution (in Angstrom) with which to sample underlying
            spectra of objects. 20 is optimized for speed and accuracy.
        window : int
            Can optionally compute magnitude as the intrinsic spectrum 
            centered at `wave` but convolved with a `window`-pixel boxcar.
            
            
        """
        if presets is not None:
            filter_set = None
            cam, filters = self._get_presets(z, presets)
        
        if type(filters) is dict:
            filters = filters[round(z)]
        
        # Don't put any binning stuff in here!
        kw = {'z': z, 'cam': cam, 'filters': filters, 'window': window,
            'filter_set': filter_set, 'dlam':dlam, 'method': method,
            'wave': wave}
        
        dL = self.cosm.LuminosityDistance(z) / cm_per_pc
        magcorr = 5. * (np.log10(dL) - 1.)
        
        kw_tup = tuple(kw.items())
        
        if load:
            cached_result = self._cache_mags(kw_tup)
        else:
            cached_result = None
            
        if cached_result is not None:
            #print("Mag load from cache:", wave, window)
            M, mags = cached_result
        else:
            # Take monochromatic (or within some window) MUV     
            L = self.Luminosity(z, wave=wave, window=window)
            M = self.magsys.L_to_MAB(L, z=z)
            
            ##
            # Compute magnitude from photometry
            if (filters is not None) or (filter_set is not None):
                assert cam is not None
                
                hist = self.histories
                
                if type(cam) not in [tuple, list]:
                    cam = [cam]
                
                mags = []
                xph  = []
                fil = []
                for j, _cam in enumerate(cam):
                                    
                    _filters, xphot, dxphot, ycorr = \
                        self.synth.Photometry(zobs=z, sfh=hist['SFR'], zarr=hist['z'],
                            hist=hist, dlam=dlam, cam=_cam, filters=filters, 
                            filter_set=filter_set, idnum=idnum, extras=self.extras,
                            rest_wave=None)
            
                    mags.extend(list(np.array(ycorr) - magcorr))
                    xph.extend(xphot)
                    fil.extend(_filters)
            
                mags = np.array(mags)
                
            else:
                mags = M
                
            if hasattr(self, '_cache_mags_'):
                self._cache_mags_[kw_tup] = M, mags    
            
        ##
        # Interpolate etc.
        ##    
        if (filters is not None) or (filter_set is not None):    
            hist = self.histories
            if method == 'gmean':
                if len(mags) == 0:
                    Mg = -99999 * np.ones(hist['SFR'].shape[0])
                else:    
                    Mg = -1 * np.nanprod(np.abs(mags), axis=0)**(1. / float(len(mags)))
            elif method == 'closest':
                if len(mags) == 0:
                    Mg = -99999 * np.ones(hist['SFR'].shape[0])
                else:    
                    # Get closest to specified rest-wavelength
                    rphot = np.array(xph) * 1e4 / (1. + z)
                    k = np.argmin(np.abs(rphot - wave))
                    Mg = mags[k,:]
            elif method == 'interp':
                if len(mags) == 0:
                    Mg = -99999 * np.ones(hist['SFR'].shape[0])
                else:    
                    rphot = np.array(xph) * 1e4 / (1. + z)
                    kall = np.argsort(np.abs(rphot - wave))
                    _k1 = kall[0]#np.argmin(np.abs(rphot - wave))

                    if len(kall) == 1:
                        Mg = mags[_k1,:]
                    else:    
                        _k2 = kall[1]
                        
                        if rphot[_k2] < rphot[_k1]:
                            k1 = _k2
                            k2 = _k1
                        else:
                            k1 = _k1
                            k2 = _k2

                        #print(rphot)
                        #print(k1, k2, mags.shape, rphot.shape, rphot[k1], rphot[k2])
                        
                        dy = mags[k2,:] - mags[k1,:]
                        dx = rphot[k2] - rphot[k1]
                        m = dy / dx
                            
                        Mg = mags[k1,:] + m * (wave - rphot[k1])
                       
            elif method == 'mono':
                if len(mags) == 0:
                    Mg = -99999 * np.ones(hist['SFR'].shape[0])
                else:    
                    Mg = M                                
            else:
                raise NotImplemented('method={} not recognized.'.format(method))
                
            if MUV is not None:
                Mout = np.interp(MUV, M[-1::-1], Mg[-1::-1])
            else:
                Mout = Mg                    
        else:
            Mout = mags
                    
        return Mout
            
    def Luminosity(self, z, wave=1600., band=None, idnum=None, window=1,
        load=True, use_cache=True):
        """
        Return the luminosity for one or all sources at wavelength `wave`.
        
        Parameters
        ----------
        z : int, float
            Redshift of observation.
        wave : int, float
            Rest wavelength of interest [Angstrom]
        band : tuple
            Can alternatively request the average luminosity in some wavelength
            interval (again, rest wavelengths in Angstrom).
        window : int
            Can alternatively retrive the average luminosity at specified
            wavelength after smoothing intrinsic spectrum with a boxcar window
            of this width (in pixels).
        idnum : int
            If supplied, will only determine the luminosity for a single object
            (the one at this position in the array).
        cache : bool
            If False, don't save luminosities to cache. Needed sometimes to
            conserve memory if, e.g., computing luminosity for a ton of 
            wavelengths for many (millions) of halos.
        
        Returns
        -------
        Luminosity (or luminosities if idnum=None) of object(s) in the model.
        
            
        """
        cached_result = self._cache_L((z, wave, band, idnum, window))
        if load and (cached_result is not None):
            return cached_result
            
        if band is not None:
            assert self.pf['pop_dust_yield'] in [0, None], \
                "Going to get weird answers for L(band != None) if dust is ON."
        
        raw = self.histories
        L = self.synth.Luminosity(wave=wave, zobs=z, hist=raw, 
            extras=self.extras, idnum=idnum, window=window, load=load,
            use_cache=use_cache, band=band)
           
        if use_cache:
            self._cache_L_[(z, wave, band, idnum, window)] = L.copy()
           
        return L
            
    def LuminosityFunction(self, z, x, mags=True, wave=1600., window=1, 
        band=None):
        """
        Compute the luminosity function from discrete histories.
        
        Need to be a little careful about indexing here. For example, if we
        request the LF at a redshift not present in the grid, we need to...
        
        """
        
        cached_result = self._cache_lf(z, x, wave)
        if cached_result is not None:
            return cached_result
                                
        # These are kept in descending redshift just to make life difficult.
        # [The last element corresponds to observation redshift.]
        raw = self.histories
                        
        keys = raw.keys()
        Nt = raw['t'].size
        Nh = raw['Mh'].shape[0]
        
        # Find the z grid point closest to that requested.
        # Must be >= z requested.
        izobs = np.argmin(np.abs(raw['z'] - z))
        if z > raw['z'][izobs]:
            # Go to one grid point lower redshift
            izobs += 1

        izobs = min(izobs, len(raw['z']) - 2)                   

        ##
        # Run in batch.
        L = self.Luminosity(z, wave=wave, band=band, window=window)
        ##    
        
        zarr = raw['z']
        tarr = raw['t']

        # Need to be more careful here as nh can change when using
        # simulated halos
        w = raw['nh'][:,izobs+1]
                                                        
        _MAB = self.magsys.L_to_MAB(L, z=z)
        
        if self.pf['dustcorr_method'] is not None:
            MAB = self.dust.Mobs(z, _MAB)
        else:
            MAB = _MAB

        # If L=0, MAB->inf. Hack these elements off if they exist.
        # This should be a clean cut, i.e., there shouldn't be random
        # spikes where L==0, all L==0 elements should be a contiguous chunk.
        Misok = np.logical_and(L > 0, np.isfinite(L))
        
        # Always bin to setup cache, interpolate from then on.
        _x = np.arange(-28, 5., self.pf['pop_mag_bin'])

        hist, bin_edges = np.histogram(MAB[Misok==1],
            weights=w[Misok==1], bins=bin_c2e(_x), density=True)
            
        bin_c = _x
        
        N = np.sum(w[Misok==1])
        
        phi = hist * N
                
        self._cache_lf_[(z, wave)] = bin_c, phi
        
        return self._cache_lf(z, x, wave)
        
    def _cache_beta(self, kw_tup):
    
        if not hasattr(self, '_cache_beta_'):
            self._cache_beta_ = {}
            
        if kw_tup in self._cache_beta_:
            return self._cache_beta_[kw_tup]
            
        return None
    
    def _cache_mags(self, kw_tup):
    
        if not hasattr(self, '_cache_mags_'):
            self._cache_mags_ = {}
            
        if kw_tup in self._cache_mags_:
            return self._cache_mags_[kw_tup]
            
        return None    
        
    @property
    def extras(self):
        if not hasattr(self, '_extras'):
            if self.pf['pop_dust_yield'] is not None:
                self._extras = {'kappa': self.guide.dust_kappa}
            else:
                self._extras = {}
        return self._extras
        
    def _get_presets(self, z, presets, for_beta=True, wave_range=None):
        """
        Convenience routine to retrieve `cam` and `filters` via short-hand.
        
        Parameters
        ----------
        z : int, float
            Redshift of interest.
        presets : str
            Name of presets package to use, e.g., 'hst', 'jwst', 'hst+jwst'.
        for_beta : bool 
            If True, will restrict to filters used to compute UV slope.
        wave_range : tuple  
            If for_beta==False, can restrict photometry to specified range
            of rest wavelengths (in Angstroms). If None, will return all
            photometry.
        
        Returns
        -------
        Tuple containing (i) list of cameras, and (ii) list of filters.
        
        """
        
        zstr = int(round(z))
        
        if ('hst' in presets.lower()) or ('hubble' in presets.lower()):
            hst_shallow = self._b14.filt_shallow
            hst_deep = self._b14.filt_deep
            
            if zstr >= 7:
                filt_hst = hst_deep
            else:
                filt_hst = hst_shallow
                
        if ('jwst' in presets.lower()) or ('nircam' in presets.lower()):
            nircam_W, nircam_M = self._nircam
        
        ##
        # Hubble only
        if presets.lower() in ['hst', 'hubble']:
            if for_beta:
                cam = ('wfc', 'wfc3')
                filters = filt_hst[zstr]
            else:
                raise NotImplemented('help')
        
        # JWST only    
        elif presets.lower() in ['nircam', 'jwst']:
            
            if for_beta:
                # Override
                if z < 5:
                    raise ValueError("JWST too red for UV slope measurements at z<5!")
                
                cam = ('nircam',)
                
                wave_lo, wave_hi = np.min(self._c94), np.max(self._c94)
                
                filters = list(what_filters(z, nircam_M, wave_lo, wave_hi))
                filters.extend(list(what_filters(z, nircam_M, wave_lo, wave_hi)))
                filters = tuple(filters)
            else:
                 raise NotImplemented('help')
        
        # Combo    
        elif presets == 'hst+jwst':
            
            if for_beta:
                cam = ('wfc', 'wfc3', 'nircam') if zstr <= 8 else ('nircam', )
                filters = filt_hst[zstr] if zstr <= 8 else None
                
                if filters is not None:
                    filters = list(filters)
                else:
                    filters = []
                
                wave_lo, wave_hi = np.min(self._c94), np.max(self._c94)
                filters.extend(list(what_filters(z, nircam_M, wave_lo, wave_hi)))
                filters.extend(list(what_filters(z, nircam_M, wave_lo, wave_hi)))
                filters = tuple(filters)
            else:
                 raise NotImplemented('help')    
                    
        elif presets.lower() in ['c94', 'calzetti', 'calzetti1994']:
            return ('calzetti', ), self._c94
        else:
            raise NotImplemented('No presets={} option yet!'.format(presets))
        
        ##
        # Done!
        return cam, filters
        
    def Beta(self, z, waves=None, rest_wave=None, cam=None,
        filters=None, filter_set=None, dlam=20., method='linear', magmethod='gmean',
        return_binned=False, Mbins=None, Mwave=1600., MUV=None, Mstell=None,
        return_scatter=False, load=True, massbins=None, return_err=False,
        presets=None):
        """
        UV slope for all objects in model.

        Parameters
        ----------
        z : int, float
            Redshift.
        MUV : int, float, np.ndarray
            Optional. Set of magnitudes at which to return Beta.
            Note: these need not be at the same wavelength as that used
                  to compute Beta (see Mwave).
        wave : int, float
            Wavelength at which to compute Beta.
        Mwave : int, float
            Wavelength assumed for input MUV. Allows us to return, e.g., the
            UV slope at 2200 Angstrom corresponding to input 1600 Angstrom
            magnitudes.
        dlam : int, float
            Interval over which to average UV slope [Angstrom]
        return_binned : bool
            If True, return binned version of MUV(Beta), including 
            standard deviation of Beta in each MUV bin.

        Returns
        -------
        if MUV is None:
            Tuple containing (magnitudes, slopes)
        else:
            Slopes at corresponding user-supplied MUV (assumed to be at 
            wavelength `wave_MUV`).
        """
        
        if presets is not None:
            cam, filters = self._get_presets(z, presets)
                
        if type(filters) is dict:
            filters = filters[round(z)]
        
        # Don't put any binning stuff in here!
        kw = {'z':z, 'waves':waves, 'rest_wave':rest_wave, 'cam': cam, 
            'filters': filters, 'filter_set': filter_set,
            'dlam':dlam, 'method': method, 'magmethod': magmethod}
        
        kw_tup = tuple(kw.items())

        if load:
            cached_result = self._cache_beta(kw_tup)            
        else:
            cached_result = None

        if cached_result is not None:
            if len(cached_result) == 2:
                beta_r, beta_rerr = cached_result
            else:
                beta_r = cached_result
                
        else:
            raw = self.histories
          
            ##
            # Run in batch.
            _beta_r = self.synth.Slope(zobs=z, sfh=raw['SFR'], waves=waves, 
                zarr=raw['z'], hist=raw, dlam=dlam, cam=cam, filters=filters, 
                filter_set=filter_set, rest_wave=rest_wave, method=method, 
                extras=self.extras, return_err=return_err)
                                                            
            if return_err:
                beta_r, beta_rerr = _beta_r
            else:
                beta_r = _beta_r

            ##
            if hasattr(self, '_cache_beta_'):    
                self._cache_beta_[kw_tup] = _beta_r
                
        # Can return binned (in MUV) version
        if return_binned:
            if Mbins is None:
                Mbins = np.arange(-30, -10, 1.)

            nh = self.get_field(z, 'nh')
            
            # This will be the geometric mean of magnitudes in `cam` and
            # `filters` if they are provided!
            if (presets == 'calzetti') or (cam == 'calzetti'):
                assert magmethod == 'mono', \
                    "Known issues with magmethod!='mono' and Calzetti approach."
  
            _MAB = self.Magnitude(z, wave=Mwave, cam=cam, filters=filters,
                method=magmethod, presets=presets)
                                        
            MAB, beta, _std, N1 = bin_samples(_MAB, beta_r, Mbins, weights=nh)
                        
        else:
            beta = beta_r

        if MUV is not None:
            return np.interp(MUV, MAB, beta, left=-99999, right=-99999)
        if Mstell is not None:
            Ms_r = self.get_field(z, 'Ms')
            nh_r = self.get_field(z, 'nh')
            x1, y1, err1, N1 = bin_samples(np.log10(Ms_r), beta_r, massbins, 
                weights=nh_r)
            return np.interp(np.log10(Mstell), x1, y1, left=0., right=0.)
        
        # Get out.
        if return_scatter:
            return beta, _std
        
        if return_err:
            assert not return_scatter
            assert not return_binned
            
            return beta, beta_rerr
        else:    
            return beta
        
    def AUV(self, z, Mwave=1600., cam=None, MUV=None, Mstell=None, magbins=None, 
        massbins=None, return_binned=False, filters=None, dlam=20.):
        """
        Compute UV extinction.
        
        Parameters
        ----------
        z : int, float
            Redshift.
        Mstell : int, float, np.ndarray
            Can return AUV as function of provided stellar mass.
        MUV : int, float, np.ndarray
            Can also return AUV as function of UV magnitude.
        magbins : np.ndarray
            Either the magnitude bins or log10(stellar mass) bins to use if
            return_binned==True.
        
        """
        
        if self.pf['pop_dust_yield'] is None:
            return None
        
        Mh = self.get_field(z, 'Mh')
        kappa = self.guide.dust_kappa(wave=Mwave, Mh=Mh)
        Sd = self.get_field(z, 'Sd')
        tau = kappa * Sd
        
        AUV_r = np.log10(np.exp(-tau)) / -0.4
            
        # Just do this to get MAB array of same size as Mh
        MAB = self.Magnitude(z, wave=Mwave, cam=cam, filters=filters, dlam=dlam)
                
        if return_binned:            
            if magbins is None:
                magbins = np.arange(-30, -10, 0.25)

            nh = self.get_field(z, 'nh')
            _x, _y, _z, _N = bin_samples(MAB, AUV_r, magbins, weights=nh)

            MAB = _x
            AUV = _y
            std = _z
        else:
            #MAB = np.flip(MAB)
            #beta = np.flip(beta)
            std = None
            AUV = AUV_r

            assert MUV is None

        # May specify a single magnitude at which to return AUV        
        if MUV is not None:
            return np.interp(MUV, MAB, AUV, left=0., right=0.)
        if Mstell is not None:
            Ms_r = self.get_field(z, 'Ms')
            nh_r = self.get_field(z, 'nh')
            
            x1, y1, err1, N1 = bin_samples(np.log10(Ms_r), AUV_r, massbins, 
                weights=nh_r)
            return np.interp(np.log10(Mstell), x1, y1, left=0., right=0.)

        # Otherwise, return raw (or binned) results
        return AUV
        
    def dBeta_dMstell(self, z, dlam=20., Mstell=None, massbins=None):
        """
        Compute gradient in UV color at fixed stellar mass.
        
        Parameters
        ----------
        z : int, float
            Redshift of interest.
        dlam : int
            Wavelength resolution to use for Beta calculation.
        Mstell : int, float, np.ndarray
            Stellar mass at which to evaluate slope. Can be None, in which 
            case we'll return over a grid with resolution 0.5 dex.
        """
        
        zstr = round(z)
        calzetti = read_lit('calzetti1994').windows
        
        # Compute raw beta and compare to Mstell    
        beta_c94 = self.Beta(z, Mwave=1600., return_binned=False,
            cam='calzetti', filters=calzetti, dlam=dlam, rest_wave=None,
            magmethod='mono')

        Ms_r = self.get_field(z, 'Ms')
        nh_r = self.get_field(z, 'nh')

        # Compute binned version of Beta(Mstell).
        _x1, _y1, _err, _N = bin_samples(np.log10(Ms_r), beta_c94, massbins, 
            weights=nh_r)

        # Compute slopes with Mstell
        popt, pcov = curve_fit(_linfunc, _x1, _y1, p0=[0.3, 0.], maxfev=100)
        popt2, pcov2 = curve_fit(_cubfunc, _x1, _y1, p0=[0.0, 0.3, 0.], 
            maxfev=1000)
        cubrecon = popt2[0] * (_x1 - 8.)**2 + popt2[1] * _x1 + popt2[2]
        cubeder = 2 * popt2[0] * (_x1 - 8.) + popt2[1]
             
        norm = [] 
        dBMstell = []
        for _x in _x1:
            dBMstell.append(np.interp(_x, _x1, cubeder))
        
        if Mstell is None:
            return np.array(_x1), np.array(dBMstell)
        else:
            return np.interp(np.log10(Mstell), _x1, dBMstell)
    
    def dColor_dz(self, logM, dlam=1., zmin=4, zmax=10, dz=1):
        
        out = []
        zarr = np.arange(zmin, zmax+dz, dz)
        for z in zarr:
            _logM, _slope = self.dColor_dMstell(z, dlam=dlam)
            out.append(np.interp(logM, _logM, _slope))
            
        
    def Gradient(self, field, wrt, as_func_of, eval_at_x, eval_at_y, ybins,
        guess=[0., 1.5]):
        """
        Calculate derivatives. Generally fit with linear or PL function first.
        """
        
        if field in self.histories.keys():
            y = self.get_field(z, field)
        else:
            assert wrt == 'z', "only option right now"
            if field == 'AUV':
                if as_func_of == 'Ms':
                    y = []
                    for z in eval_at_x:
                        _y = self.AUV(z=z, Mstell=eval_at_y, massbins=ybins)
                        y.append(_y)
                    y = np.array(y)
                else:
                    raise NotImplemented('help')
            else:
                raise NotImplemented('help')
        
        ##
        # Get on with the fitting
        ##
        x = eval_at_x
        
        func = lambda x, p0, p1: p0 * (x - 4.) + p1
        
        if type(eval_at_y) in [int, float, np.float64]:
            popt, pcov = curve_fit(func, x, y, p0=guess, maxfev=100)
            return x, popt[0]
        
        slopes = []
        for k, element in enumerate(eval_at_y):
            popt, pcov = curve_fit(func, x, y[:,k], p0=guess, maxfev=100)
            slopes.append(popt[0])
            
        return x, np.array(slopes)    
        
    def get_contours(self, x, y, bins):
        """
        Take 'raw' data and recover contours. 
        """

        bin_e = bin_c2e(bins)
        
        band = []
        for i in range(len(bins)):
            ok = np.logical_and(x >= bin_e[i], x < bin_e[i+1])
            
            if ok.sum() == 0:
                band.append((-np.inf, -np.inf, -np.inf))
                continue
            
            yok = y[ok==1]
            
            y1, y2 = np.percentile(yok, (16., 84.))
            ym = np.mean(yok)
            
            band.append((y1, y2, ym))
            
        return np.array(band).T
        
    def MainSequence(self, z):
        """
        How best to plot this?
        """
        pass
        
    def SFRF(self, z):
        pass
            
    def PDF(self, z, **kwargs):
        # Look at distribution in some quantity at fixed z, potentially other stuff.
        pass
    
    def prep_hist_for_cache(self):
        keys = ['nh', 'MAR', 'Mh', 't', 'z']
        hist = {key:self.histories[key][-1::-1] for key in keys}
        return hist
    
    def load(self):
        """
        Load results from past run.
        """
                
        fn_hist = self.pf['pop_histories']
        
        # Look for results attached to hmf table
        if fn_hist is None:
            prefix = self.guide.halos.tab_prefix_hmf(True)
            fn = self.guide.halos.tab_name
        
            suffix = fn[fn.rfind('.')+1:]
            path = os.getenv("ARES") + '/input/hmf/'
            fn_hist = path + prefix.replace('hmf', 'hgh') + '.' + suffix
        else:
            # Check to see if parameters match
            if self.pf['verbose']:
                print("Should check that HMF parameters match!")
                        
        # Read output
        if type(fn_hist) is str:
            if fn_hist.endswith('.pkl'):
                f = open(fn_hist, 'rb')
                prefix = fn_hist.split('.pkl')[0]
                zall, traj_all = pickle.load(f)
                f.close()
                if self.pf['verbose']:
                    print("Loaded {}.".format(fn_hist))
                hist = traj_all
                      
            elif fn_hist.endswith('.hdf5'):
                f = h5py.File(fn_hist, 'r')
                prefix = fn_hist.split('.hdf5')[0]
                
                if 'mask' in f:
                    mask = np.array(f[('mask')])
                else:
                    mask = np.zeros_like(f[('Mh')])
                
                hist = {}
                for key in f.keys():
                                            
                    if key not in ['cosmology', 't', 'z', 'children', 'pos']:
                        #hist[key] = np.ma.array(f[(key)], mask=mask,
                        #    fill_value=-np.inf)
                        
                        # Oddly, masking causes a weird issue with a huge
                        # spike at log10(MAR) ~ 1. np.ma operations are 
                        # also considerably slower.
                        hist[key] = np.array(f[(key)]) * np.logical_not(mask)
                        
                        #else:
                        #    hist[key] = np.ma.array(f[(key)], mask=mask)
                    else:
                        hist[key] = np.array(f[(key)])
                            
                zall = hist['z']
                
                f.close()
                if self.pf['verbose']:
                    print("Loaded {}.".format(fn_hist))
                
            else:
                # Assume pickle?
                f = open(fn_hist+'.pkl', 'rb')
                prefix = fn_hist
                zall, traj_all = pickle.load(f)
                f.close()
                if self.pf['verbose']:
                    print("Loaded {}.".format(fn_hist+'.pkl'))
            
                hist = traj_all
                
                if self.pf['verbose']:
                    print("Read `pop_histories` as dictionary")
                
            hist['zform'] = zall
            hist['zobs'] = np.array([zall] * hist['nh'].shape[0])            
                
        elif type(self.pf['pop_histories']) is dict:
            hist = self.pf['pop_histories']
            # Assume you know what you're doing.
        else:
            hist = None
                
        return hist
        
    def SaveCatalog(self, prefix, redshifts=None, waves=None, fields=None,
        dlam=20., filters=None, save_spec=True):
        """
        Create a galaxy catalog over a series of redshifts.
        """
                        
        hist = self.histories
        zarr = hist['z']
        tarr = hist['t']
        
        if redshifts is None:
            zmin = round(min(zarr))
            redshifts = np.arange(zmin, 13, 1.)
        
        if waves is None:
            waves = np.arange(40., 3000.+dlam, dlam)
        
        if fields is None:
            fields = 'Mh', 'Ms', 'SFR', 'Md'
            # Also, MUV, beta....
        
        for i, z in enumerate(redshifts):
            
            _iz = np.argmin(np.abs(z - zarr))
            if zarr[_iz] > z:
                _iz -= 1
            zactual = zarr[_iz]    
            
            ##
            # Save spectra
            if save_spec:
                spec = self.synth.Spectrum(waves, sfh=hist['SFR'], zarr=zarr, 
                    window=1, zobs=zactual, units='Hz', hist=hist)
                    
                fn_spec = '{}.z={}.spec.hdf5'.format(prefix, zactual)    
                    
                with h5py.File(fn_spec, 'w') as f:
                    ds = f.create_dataset('spec', data=spec)
                    ds = f.create_dataset('wave', data=waves)
                print("Wrote {}.".format(fn_spec))
            
            ##
            # Save basics
            fn_prop = '{}.z={}.prop.hdf5'.format(prefix, zactual)    
                
            with h5py.File(fn_prop, 'w') as f:
                pass
            print("Wrote {}.".format(fn_prop))    
            
            if filters is None:
                continue
                
            fn_phot = '{}.z={}.phot.hdf5'.format(prefix, zactual)    
            with h5py.File(fn_phot, 'w') as f:
                pass
            print("Wrote {}.".format(fn_phot))
            
            
        # Save files with galaxy properties ('prop') and spectra ('spec').
        # Could derive photometry and beta later.
            
    
    def save(self, prefix, clobber=False):
        """
        Output model (i.e., galaxy trajectories) to file.
        """
        
        fn = prefix + '.pkl'
                
        if os.path.exists(fn) and (not clobber):
            raise IOError('File \'{}\' exists! Set clobber=True to overwrite.'.format(fn))
        
        zall, traj_all = self._gen_halo_histories()

        f = open(fn, 'wb')
        pickle.dump((zall, traj_all), f)
        f.close()
        print("Wrote {}".format(fn))
        
        # Also save parameters.
        f = open('{}.parameters.pkl'.format(prefix))
        pickle.dump(self.pf)
        f.close()
        
        