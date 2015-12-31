"""

BlobFactory.py

Author: Jordan Mirocha
Affiliation: University of Colorado at Boulder
Created on: Fri Dec 11 14:24:53 PST 2015

Description: 

"""

import re
import numpy as np

try:
    import cPickle as pickle
except ImportError:
    import pickle
    
# Some standard blobs    
    
    
class default_blobs(object):
    def __init__(self):
        blobs_1d = ['igm_dTb', 'igm_Ts', 'igm_Tk', 'cgm_h_2', 'igm_h_1', 
            'igm_k_heat_h_1', 'cgm_k_ion_h_1']
        blobs_scalar = ['z_B', 'z_C', 'z_D']
        for key in blobs_1d:    
            for tp in list('BCD'):
                blobs_scalar.append('%s_%s' % (key, tp))
                
        self.blob_names = [blobs_scalar, blobs_1d]
        self.blob_ivars = [None, np.arange(5, 41, 1)]
    
    
def get_k(s):
    m = re.search(r"\[(\d+(\.\d*)?)\]", s)
    return int(m.group(1))
    
def parse_attribute(blob_name, obj_base):
    """
    Find the attribute nested somewhere in an object that we need to compute
    the value of blob `blob_name`.
    """
    
    attr_split = blob_name.split('.')

    if len(attr_split) == 1: 
        s = attr_split[0]
        if re.search('\[', s):     
            k = get_k(s)
            return obj_base.__getattribute__(s[0:s.rfind('[')])[k]
        else:
            return obj_base.__getattribute__(s)

    # Nested attribute
    blob = None
    obj_list = [obj_base]
    for i in range(len(attr_split)):
        s = attr_split[i]

        # Brackets indicate...?
        if re.search('\[', s): 
            k = get_k(s)
            blob = obj_list[i].__getattribute__(s[0:s.rfind('[')])[k]
            
            if (i == (len(attr_split) - 1)):
                break
            else:
                new_obj = blob
                blob = None
        else:
            new_obj = obj_list[i].__getattribute__(s)

        obj_list.append(new_obj)

    if blob is None:
        blob = new_obj
        
    return blob

class BlobFactory(object):
    """
    This class must be inherited by another class, which need only have the
    ``pf`` attribute.
    
    The three most (only?) important parameters are:
        blob_names
        blob_ivars
        blob_funcs
        
    """

    def _parse_blobs(self):
        try:
            names = self.pf['blob_names']
        except KeyError:
            names = None

        if names is None:
            self._blob_names = self._blob_ivars = None
            self._blob_dims = self._blob_nd = None
            self._blob_funcs = None
            return None
        else:
            # Otherwise, figure out how many different kinds (shapes) of
            # blobs we have
            assert type(names) in [list, tuple], \
                "Must supply blob_names as list or tuple!"

            self._blob_names = names
            self._blob_ivars = self.pf['blob_ivars']

            self._blob_nd = []
            self._blob_dims = []
            self._blob_funcs = []
            for i, element in enumerate(self._blob_names):
                
                # Scalars
                if np.isscalar(self._blob_ivars[i]) or \
                   (self._blob_ivars[i] is None):
                    self._blob_nd.append(0)
                    self._blob_dims.append(0)
                # Everything else
                else:
                    
                    # Be careful with 1-D
                    if type(self._blob_ivars[i]) is np.ndarray:
                        lenarr = len(self._blob_ivars[i].shape)
                        assert lenarr == 1
                        
                        self._blob_nd.append(1)
                        self._blob_dims.append(lenarr)
                    else:

                        self._blob_nd.append(len(self._blob_ivars[i]))
                        self._blob_dims.append([len(element) \
                            for element in self._blob_ivars[i]])
                
                # Handle functions
                if self.pf['blob_funcs'] is None:
                    self._blob_funcs.append([None] * len(element))
                elif self._blob_dims[i] == 1 and self.pf['blob_funcs'] is None:
                    self._blob_funcs.append([None] * len(element))
                else:
                    self._blob_funcs.append(self.pf['blob_funcs'][i])

        self._blob_nd = tuple(self._blob_nd)                    
        self._blob_dims = tuple(self._blob_dims)            
        self._blob_names = tuple(self._blob_names)
        self._blob_ivars = tuple(self._blob_ivars)
        self._blob_funcs = tuple(self._blob_funcs)
    
    @property
    def blob_groups(self):
        if not hasattr(self, '_blob_groups'):
            self._blob_groups = len(self.blob_nd)
        return self._blob_nd
                
    @property
    def blob_nd(self):    
        if not hasattr(self, '_blob_nd'):
            self._parse_blobs()
        return self._blob_nd
    
    @property
    def blob_dims(self):    
        if not hasattr(self, '_blob_dims'):
            self._parse_blobs()
        return self._blob_dims    
        
    @property
    def blob_names(self):
        if not hasattr(self, '_blob_names'):
            self._parse_blobs()
        return self._blob_names    
            
    @property
    def blob_ivars(self):
        if not hasattr(self, '_blob_ivars'):
            self._parse_blobs()
        return self._blob_ivars
        
    @property
    def blob_funcs(self):
        if not hasattr(self, '_blob_funcs'):
            self._parse_blobs()
        return self._blob_funcs

    @property
    def blobs(self):
        if not hasattr(self, '_blobs'):
            self._generate_blobs()    
    
        return self._blobs
        
    def get_blob(self, name, ivar=None):
        for i in self.blob_groups:
            for j, blob in enumerate(self.blob_names[i]):
                if blob == name:
                    break
            
            if blob == name:
                break        
                    
        if self.blob_nd[i] > 0 and (ivar is None):
            raise ValueError('Must provide ivar!')
        elif self.blob_nd[i] == 0:
            return float(self.blobs[i])
        elif self.blob_nd[i] == 1:
            assert ivar in self.blob_ivars[i]
            
            raise NotImplemented('help')
            
        elif self.blob_nd[i] == 2:
            assert len(ivar) == 2
            # also assert that both values are in self.blob_ivars!
            # Actually, we don't have to abide by that. As long as a function
            # is provided we can evaluate the blob anywhere (with interp)

            raise NotImplemented('help')            
            
    def _generate_blobs(self):
        """
        Create a list of blobs, one per blob group.
        
        ..note:: This should only be run for individual simulations,
            not in the analysis of MCMC data.
        
        Returns
        -------
        List, where each element has shape (ivar x blobs). Each element of 
        this corresponds to the blobs for one blob group, which is defined by
        either its dimensionality, its independent variables, or both.
        
        For example, for 1-D blobs, self.blobs[i][j][k] would mean
            i = blob group
            j = index corresponding to elements of self.blob_names
            k = index corresponding to elements of self.blob_ivars[i]
        """
        
        self._blobs = []
        for i, element in enumerate(self.blob_names):
                        
            this_group = []
            for j, key in enumerate(element):
                                
                # 0-D blobs. Need to know name of attribute where stored!
                if self.blob_nd[i] == 0:
                    if self.blob_funcs[i][j] is None:
                        # Assume blob name is the attribute
                        #blob = self.__getattribute__(key)
                        blob = parse_attribute(key, self)
                    else:
                        fname = self.blob_funcs[i][j]
                        func = parse_attribute(fname, self)
                        blob = func(self.blob_ivars[i])

                # 1-D blobs. Assume the independent variable is redshift.
                elif self.blob_nd[i] == 1:
                    x = np.array(self.blob_ivars[i])
                    if (self.blob_funcs[i][j] is None):
                        blob = np.interp(x, self.history['z'][-1::-1], 
                            self.history[key][-1::-1])
                    else:
                        fname = self.blob_funcs[i][j]
                        func = parse_attribute(fname, self)
                        blob = np.array(map(func, x))
                else:
                    # Must have blob_funcs for this case
                    fname = self.blob_funcs[i][j]
                    func = parse_attribute(fname, self)
                    
                    xarr, yarr = map(np.array, self.blob_ivars[i])
                    blob = []
                    for i, x in enumerate(xarr):
                        tmp = []
                        for j, y in enumerate(yarr):
                            tmp.append(func(x, y))
                        blob.append(tmp)

                this_group.append(np.array(blob))

            self._blobs.append(np.array(this_group))
            
    @property 
    def blob_data(self):
        if not hasattr(self, '_blob_data'):
            self._blob_data = {}
        return self._blob_data
    
    @blob_data.setter
    def blob_data(self, value):
        self._blob_data.update(value)    
    
    def get_blob_from_disk(self, name):
        return self.__getitem__(name)
    
    def __getitem__(self, name):
        if name in self.blob_data:
            return self.blob_data[name]
        
        return self._get_item(name)
    
    def blob_info(self, name):
        """
        Returns
        -------
        index of blob group, index of element within group, dimensionality, 
        and exact dimensions of blob.
        """
        found = False
        for i, group in enumerate(self.blob_names):
            for j, element in enumerate(group):
                if element == name:
                    found = True
                    break            
            if element == name:
                break
                
        if not found:
            raise KeyError('Blob %s not found.' % name)        
                
        return i, j, self.blob_nd[i], self.blob_dims[i]
    
    def _get_item(self, name):
        
        i, j, nd, dims = self.blob_info(name)
    
        fn = "%s.blob_%id.%s.pkl" % (self.prefix, nd, name)
        
        f = open(fn, 'rb')
    
        all_data = []
        while True:
            try:
                data = pickle.load(f)
            except EOFError:
                break
    
            all_data.extend(data)
    
        all_data = np.array(all_data)
        
        mask = np.logical_not(np.isfinite(all_data))
        masked_data = np.ma.array(all_data, mask=mask)
        
        self.blob_data = {name: masked_data}
        
        return masked_data
    
    
    