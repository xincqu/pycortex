import os

import nibabel
import numpy as np
from scipy import sparse
from itertools import product

import polyutils
from db import surfs

class Mapper(object):
    '''Maps data from epi volume onto surface using various projections'''
    def __init__(self, subject, xfmname, recache=False, **kwargs):
        self.idxmap = None
        self.subject, self.xfmname = subject, xfmname
        fnames = surfs.getFiles(subject)
        ptype = self.__class__.__name__.lower()
        kwds ='_'.join(['%s%s'%(k,str(v)) for k, v in kwargs.items()])
        if len(kwds) > 0:
            ptype += '_'+kwds
        self.cachefile = fnames['projcache'].format(xfmname=xfmname, projection=ptype)

        xfm, epifile = surfs.getXfm(subject, xfmname)
        nib = nibabel.load(epifile)
        self.shape = nib.get_shape()[:3][::-1]

        xfmfile = fnames['xfms'].format(xfmname=xfmname)
        try:
            npz = np.load(self.cachefile)
            if recache or os.stat(self.cachefile).st_mtime < os.stat(xfmfile).st_mtime:
                raise IOError

            left = (npz['left_data'], npz['left_indices'], npz['left_indptr'])
            right = (npz['right_data'], npz['right_indices'], npz['right_indptr'])
            lsparse = sparse.csr_matrix(left, shape=npz['left_shape'])
            rsparse = sparse.csr_matrix(right, shape=npz['right_shape'])
            self.masks = [lsparse, rsparse]
            self.nverts = lsparse.shape[0] + rsparse.shape[0]
        except IOError:
            self._recache(subject, xfmname, **kwargs)

    @property
    def mask(self):
        mask = np.array(self.masks[0].sum(0) + self.masks[1].sum(0))
        return (mask.squeeze() != 0).reshape(self.shape)

    @property
    def hemimasks(self):
        func = lambda m: (np.array(m.sum(0)).squeeze != 0).reshape(self.shape)
        return map(func, self.masks)

    def __repr__(self):
        ptype = self.__class__.__name__
        return '<%s mapper for (%s, %s) with %d vertices>'%(ptype, self.subject, self.xfmname, self.nverts)

    def __call__(self, data):
        if self.nverts in data.shape:
            llen = self.masks[0].shape[0]
            left, right = data[..., :llen], data[..., llen:]

            if self.idxmap is not None:
                return left[..., self.idxmap[0]], right[..., self.idxmap[0]]
            return left, right
            

        if data.ndim in (1, 3):
            data = data[np.newaxis]

        mapped = []
        for mask in self.masks:
            if self.mask.sum() in data.shape:
                shape = (np.prod(self.shape), data.shape[0])
                norm = np.zeros(shape)
                norm[self.mask.ravel()] = data.T
            elif data.ndim == 4:
                norm = data.reshape(len(data), -1).T
            else:
                raise ValueError

            mapped.append(np.array(mask * norm).T.squeeze())

        if self.idxmap is not None:
            mapped[0] = mapped[0][..., self.idxmap[0]]
            mapped[1] = mapped[1][..., self.idxmap[1]]

        return mapped
        
    def backwards(self, verts):
        '''Projects vertex data back into volume space

        Parameters
        ----------
        verts : array_like
            If uint array and max <= nverts, assume binary mask of vertices
            If float array and len == nverts, project float values into volume
        '''
        left = np.zeros((self.masks[0].shape[0],), dtype=bool)
        right = np.zeros((self.masks[1].shape[0],), dtype=bool)
        if isinstance(verts, (list, tuple)) and len(verts) == 2:
            if len(verts[0]) == len(left):
                left = verts[0]
                right = verts[1]
            elif verts[0].max() < len(left):
                left[verts[0]] = True
                right[verts[1]] = True
            else:
                raise ValueError
        else:
            if len(verts) == self.nverts:
                left = verts[:len(left)]
                right = verts[len(left):]
            elif verts.max() < self.nverts:
                left[verts[verts < len(left)]] = True
                right[verts[verts >= len(left)] - len(left)] = True
            else:
                raise ValueError

        output = []
        for mask, data in zip(self.masks, [left, right]):
            proj = data * mask
            output.append(np.array(proj).reshape(self.shape))

        return output

    def _recache(self, left, right):
        self.nverts = left.shape[0] + right.shape[0]
        self.masks = [left, right]
        np.savez(self.cachefile, 
            left_data=left.data, 
            left_indices=left.indices, 
            left_indptr=left.indptr,
            left_shape=left.shape,
            right_data=right.data,
            right_indices=right.indices,
            right_indptr=right.indptr,
            right_shape=right.shape)

class Nearest(Mapper):
    '''Maps epi volume data to surface using nearest neighbor interpolation'''
    def _recache(self, subject, xfmname):
        masks = []
        coord, epifile = surfs.getXfm(subject, xfmname, xfmtype='coord')
        fid = surfs.getVTK(subject, 'fiducial', merge=False, nudge=False)
        flat = surfs.getVTK(subject, 'flat', merge=False, nudge=False)

        for (pts, _, _), (_, polys, _) in zip(fid, flat):
            valid = np.zeros((len(pts),), dtype=bool)
            valid[np.unique(polys)] = True
            coords = polyutils.transform(coord, pts).round().astype(int)
            d1 = np.logical_and(0 <= coords[:,0], coords[:,0] < self.shape[2])
            d2 = np.logical_and(0 <= coords[:,1], coords[:,1] < self.shape[1])
            d3 = np.logical_and(0 <= coords[:,2], coords[:,2] < self.shape[0])
            valid = np.logical_and(np.logical_and(valid, d1), np.logical_and(d2, d3))

            ravelidx = np.ravel_multi_index(coords.T[::-1], self.shape, mode='clip')

            ij = np.array([np.nonzero(valid)[0], ravelidx[valid]])
            data = np.ones((len(ij.T),), dtype=bool)
            csrshape = len(pts), np.prod(self.shape)
            masks.append(sparse.csr_matrix((data, ij), dtype=bool, shape=csrshape))
            
        super(Nearest, self)._recache(masks[0], masks[1])

class Trilinear(Mapper):
    def _recache(self, subject, xfmname):
        #trilinear interpolation equation from http://paulbourke.net/miscellaneous/interpolation/
        masks = []
        coord, epifile = surfs.getXfm(subject, xfmname, xfmtype='coord')
        fid = surfs.getVTK(subject, 'fiducial', merge=False, nudge=False)
        flat = surfs.getVTK(subject, 'flat', merge=False, nudge=False)

        for (pts, _, _), (_, polys, _) in zip(fid, flat):
            valid = np.unique(polys)
            coords = polyutils.transform(coord, pts[valid])
            xyz, floor = np.modf(coords.T)
            floor = floor.astype(int)
            ceil = floor + 1
            x, y, z = xyz
            x[x < 0] = 0
            y[y < 0] = 0
            z[z < 0] = 0

            i000 = np.ravel_multi_index((floor[2], floor[1], floor[0]), self.shape, mode='clip')
            i100 = np.ravel_multi_index((floor[2], floor[1],  ceil[0]), self.shape, mode='clip')
            i010 = np.ravel_multi_index((floor[2],  ceil[1], floor[0]), self.shape, mode='clip')
            i001 = np.ravel_multi_index(( ceil[2], floor[1], floor[0]), self.shape, mode='clip')
            i101 = np.ravel_multi_index(( ceil[2], floor[1],  ceil[0]), self.shape, mode='clip')
            i011 = np.ravel_multi_index(( ceil[2],  ceil[1], floor[0]), self.shape, mode='clip')
            i110 = np.ravel_multi_index((floor[2],  ceil[1],  ceil[0]), self.shape, mode='clip')
            i111 = np.ravel_multi_index(( ceil[2],  ceil[1],  ceil[0]), self.shape, mode='clip')

            v000 = (1-x)*(1-y)*(1-z)
            v100 = x*(1-y)*(1-z)
            v010 = (1-x)*y*(1-z)
            v110 = x*y*(1-z)
            v001 = (1-x)*(1-y)*z
            v101 = x*(1-y)*z
            v011 = (1-x)*y*z
            v111 = x*y*z

            i    = np.tile(valid, [8, 1]).T.ravel()
            j    = np.vstack([i000, i100, i010, i001, i101, i011, i110, i111]).T.ravel()
            data = np.vstack([v000, v100, v010, v001, v101, v011, v110, v111]).T.ravel()
            csrshape = len(pts), np.prod(self.shape)
            masks.append(sparse.csr_matrix((data, (i, j)), shape=csrshape))

        super(Trilinear, self)._recache(masks[0], masks[1])

class Lanczos(Mapper):
    def _recache(self, subject, xfmname, window=3, renorm=True):
        masks = []
        coord, epifile = surfs.getXfm(subject, xfmname, xfmtype='coord')
        nZ, nY, nX = self.shape

        fid = surfs.getVTK(subject, 'fiducial', merge=False, nudge=False)
        flat = surfs.getVTK(subject, 'flat', merge=False, nudge=False)
        
        for (pts, _, _), (_, polys, _) in zip(fid, flat):
            # valid = np.unique(polys)
            # coords = polyutils.transform(coord, pts[valid])
            coords = polyutils.transform(coord, pts)

            dx = coords[:,0] - np.atleast_2d(np.arange(nX)).T
            dy = coords[:,1] - np.atleast_2d(np.arange(nY)).T
            dz = coords[:,2] - np.atleast_2d(np.arange(nZ)).T

            def lanczos(x):
                out = np.zeros_like(x)
                sel = np.abs(x)<window
                selx = x[sel]
                out[sel] = np.sin(np.pi * selx) * np.sin(np.pi * selx / window) * (window / (np.pi**2 * selx**2))
                return out

            Lx = lanczos(dx)
            Ly = lanczos(dy)
            Lz = lanczos(dz)

            mask = sparse.lil_matrix((len(pts), np.prod(self.shape)))
            for v in range(len(pts)):
                ix = np.nonzero(Lx[:,v])[0]
                iy = np.nonzero(Ly[:,v])[0]
                iz = np.nonzero(Lz[:,v])[0]

                vx = Lx[ix,v]
                vy = Ly[iy,v]
                vz = Lz[iz,v]

                inds = np.ravel_multi_index(np.array(list(product(iz, iy, ix))).T, self.shape)
                vals = np.prod(np.array(list(product(vz, vy, vx))), 1)
                if renorm:
                    vals /= vals.sum()
                mask[v,inds] = vals

                if not v % 1000:
                    print v

            masks.append(mask.tocsr())

        super(Lanczos, self)._recache(masks[0], masks[1])

class Gaussian(Mapper):
    def _recache(self, subject, xfmname, std=2):
        raise NotImplementedError

class GaussianThickness(Mapper):
    def _recache(self, subject, xfmname, std=2):
        raise NotImplementedError

class Polyhedral(Mapper):
    def _recache(self, subject, xfmname):
        from tvtk.api import tvtk

        pia = surfs.getVTK(subject, "pia")
        wm = surfs.getVTK(subject, "whitematter")
        flat = surfs.getVTK(subject, "flat")

        coord, epifile = surfs.getXfm(subject, xfmname, xfmtype='coord')
                
        #All necessary tvtk objects for measuring intersections
        poly = tvtk.PolyData()
        voxel = tvtk.CubeSource()
        trivox = tvtk.TriangleFilter()
        trivox.set_input(voxel.get_output())
        measure = tvtk.MassProperties()
        bop = tvtk.BooleanOperationPolyDataFilter()
        bop.set_input(0, poly)
        bop.set_input(1, trivox.get_output())
        
        masks = []
        for (wpts, _, _), (ppts, _, _), (_, polys, _) in zip(pia, wm, flat):
            #iterate over hemispheres
            mask = sparse.csr_matrix((len(wpts), np.prod(self.shape)))

            tpia = polyutils.transform(coord, ppts)
            twm = polyutils.transform(coord, wpts)
            surf = polyutils.Surface(tpia, polys)
            for i, (pts, polys) in enumerate(surf.polyhedra(twm)):
                if len(pt) > 0:
                    poly.set(points=pts, polys=polys)
                    measure.set_input(poly)
                    measure.update()
                    totalvol = measure.volume

                    bmin = pt.min(0).round()
                    bmax = pt.max(0).round() + 1
                    vidx = np.mgrid[bmin[0]:bmax[0], bmin[1]:bmax[1], bmin[2]:bmax[2]]
                    for vox in vidx.reshape(3, -1).T:
                        voxel.center = vox
                        voxel.update()
                        trivox.update()
                        bop.update()
                        measure.set_input(bop.get_output())
                        measure.update()
                        if measure.volume > 1e-6:
                            idx = np.ravel_multi_index(vox[::-1], self.shape)
                            mask[i, idx] = measure.volume / totalvol

                if i % 100 == 0:
                    print i

            masks.append(mask)
        super(Polyhedral, self)._recache(masks[0], masks[1])
