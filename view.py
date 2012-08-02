import os
import time
import json
import glob
import cPickle
import numpy as np
from scipy.interpolate import interp1d
from webgl_view import show as webshow

import db

cwd = os.path.split(os.path.abspath(__file__))[0]
options = json.load(open(os.path.join(cwd, "defaults.json")))

def _gen_flat_mask(subject, height=1024):
    import polyutils
    import Image
    import ImageDraw
    pts, polys, norm = db.surfs.getVTK(subject, "flat", merge=True, nudge=True)
    pts = pts.copy()[:,:2]
    pts -= pts.min(0)
    pts *= height / pts.max(0)[1]
    im = Image.new('L', pts.max(0), 0)
    draw = ImageDraw.Draw(im)

    left, right = polyutils.trace_both(pts, polys)
    draw.polygon(pts[left], outline=None, fill=255)
    draw.polygon(pts[right], outline=None, fill=255)
    
    del draw
    return np.array(im) > 0

def _make_flat_cache(subject, xfmname, height=1024):
    from scipy.interpolate import griddata
    coords = np.vstack(db.surfs.getCoords(subject, xfmname))
    flat, polys, norm = db.surfs.getVTK(subject, "flat", merge=True, nudge=True)
    fmax, fmin = flat.max(0), flat.min(0)
    size = fmax - fmin
    aspect = size[0] / size[1]
    width = aspect * 1024

    flatpos = np.mgrid[fmin[0]:fmax[0]:width*1j, fmin[1]:fmax[1]:height*1j].reshape(2,-1)
    pcoords = griddata(flat[:,:2], coords, flatpos.T, method="nearest")
    return pcoords, (width, height)

def _get_surf_interp(subject, types=('inflated',)):
    types = ("fiducial",) + types
    pts = []
    for t in types:
        ptpolys = db.surfs.getVTK(subject, t, nudge=True)
        pts.append([p[0] for p in ptpolys])

    left, right = db.surfs.getVTK(subject, "flat", nudge=False)
    pts.append([left[0], right[0]])
    flatpolys = [p[1] for p in [left, right]]

    fidleft, fidright = db.surfs.getVTK(subject, "fiducial", nudge=True)
    fidpolys = [p[1] for p in [fidleft, fidright]]

    flatmin = 0
    for p in pts[-1]:
        flatpts = np.zeros_like(p)
        flatpts[:,[1,2]] = p[:,:2]
        #flatpts[:,0] = lt.min(0)[1]
        p[:] = flatpts
        flatmin += p[:,1].min()
    #We have to flip the left hemisphere to make it expand correctly
    pts[-1][0][:,1] = -pts[-1][0][:,1]
    #We also have to put them equally far back for pivot to line up correctly
    flatmin /= 2.
    for p in pts[-1]:
        p[:,1] -= p[:,1].min()
        p[:,1] += flatmin

    interp = [interp1d(np.linspace(0,1,len(p)), p, axis=0) for p in zip(*pts)]
    ## Store the name of each "stop" in the interpolator
    for i in interp:
        i.stops = list(types)+["flat"]

    return interp, flatpolys, fidpolys

def _tcoords(subject):
    left, right = db.surfs.getVTK(subject, "flat", hemisphere="both", nudge=True)
    fpts = np.vstack([left[0], right[0]])
    fmin = fpts.min(0)
    fpts -= fmin
    fmax = fpts.max(0)
    
    allpts = []
    for pts, polys, norms in [left, right]:
        pts -= fmin
        pts /= fmax
        allpts.append(pts[:,:2])
    return allpts

def get_mixer_args(subject, xfmname, types=('inflated',)):
    coords = db.surfs.getCoords(subject, xfmname)
    interp, flatpolys, fidpolys = _get_surf_interp(subject, types)
    
    overlay = os.path.join(options['file_store'], "overlays", "%s_rois.svg"%subject)
    if not os.path.exists(overlay):
        #Can't find the roi overlay, create a new one!
        ptpolys = db.surfs.getVTK(subject, "flat", hemisphere="both")
        pts = np.vstack(ptpolys[0][0][:,:2], ptpolys[0][1][:,:2])
        size = pts.max(0) - pts.min(0)
        aspect = size[0] / size[-1]
        with open(overlay, "w") as xml:
            xmlbase = open(os.path.join(cwd, "svgbase.xml")).read()
            xml.write(xmlbase.format(width=aspect * 1024, height=1024))

    return dict(points=interp, flatpolys=flatpolys, fidpolys=fidpolys, coords=coords,
                tcoords=_tcoords(subject), nstops=len(types)+2, svgfile=overlay)

def show(data, subject, xfm, types=('inflated',)):
    '''View epi data, transformed into the space given by xfm. 
    Types indicates which surfaces to add to the interpolater. Always includes fiducial and flat'''
    kwargs = get_mixer_args(subject, xfm, types)

    if hasattr(data, "get_affine"):
        #this is a nibabel file -- it has the nifti headers intact!
        if isinstance(xfm, str):
            kwargs['coords'] = db.surfs.getCoords(subject, xfm, hemisphere=hemisphere, magnet=data.get_affine())
        data = data.get_data()
    elif isinstance(xfm, np.ndarray):
        ones = np.ones(len(interp[0](0)))
        coords = [np.dot(xfm, np.hstack([i(0), ones]).T)[:3].T for i in interp ]
        kwargs['coords'] = [ c.round().astype(np.uint32) for c in coords ]

    kwargs['data'] = data

    import mixer
    m = mixer.Mixer(**kwargs)
    m.edit_traits()
    return m

def quickflat(data, subject, xfmname, recache=False, height=1024):
    cachename = "{subj}_{xfm}_{h}_*.pkl".format(subj=subject, xfm=xfmname, h=height)
    cachefile = os.path.join(options['file_store'], "flatcache", cachename)
    #pull a list of candidate cache files
    files = glob.glob(cachefile)
    if len(files) < 1 or recache:
        #if recaching, delete all existing files
        for f in files:
            os.unlink(f)
        print "Generating a flatmap cache"
        #pull points and transform from database
        coords, size = _make_flat_cache(subject, xfmname, height=height)
        mask = _gen_flat_mask(subject, height=height).T
        #save them into the proper file
        date = time.strftime("%Y%m%d")
        cachename = "{subj}_{xfm}_{h}_{date}.pkl".format(
            subj=subject, xfm=xfmname, h=height, date=date)
        cachefile = os.path.join(options['file_store'], "flatcache", cachename)
        cPickle.dump((coords, size, mask), open(cachefile, "w"), 2)
    else:
        coords, size, mask = cPickle.load(open(files[0]))

    ravelpos = coords[:,0]*data.shape[1]*data.shape[0]
    ravelpos += coords[:,1]*data.shape[0] + coords[:,2]
    validpos = ravelpos[mask.ravel()].astype(int)
    img = np.nan*np.ones_like(ravelpos)
    img[mask.ravel()] = data.T.ravel()[validpos]
    return img.reshape(size).T[::-1]

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Display epi data on various surfaces, \
        allowing you to interpolate between the surfaces")
    parser.add_argument("epi", type=str)
    parser.add_argument("--transform", "-T", type=str)
    parser.add_argument("--surfaces", nargs="*")
