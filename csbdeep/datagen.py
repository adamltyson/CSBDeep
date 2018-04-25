from __future__ import print_function, unicode_literals, absolute_import, division
from six.moves import range, zip, map, reduce, filter

import numpy as np
from tifffile import imread
from collections import namedtuple
import sys, warnings

from tqdm import tqdm
from .utils import Path, normalize_mi_ma, _raise, consume, compose, shuffle_inplace


## Transforms (to be added later)

class Transform(namedtuple('Transform',('name','generator','size'))):
    """Extension of :func:`collections.namedtuple` with three fields: `name`, `generator`, and `size`.

    Parameters
    ----------
    name : str
        Name of the applied transformation.
    generator : function
        Function that takes a generator as input and itself returns a generator; input and returned
        generator have the same structure as that of :class:`RawData`.
        The purpose of the returned generator is to augment the images provided by the input generator
        through additional transformations.
        It is important that the returned generator also includes every input tuple unchanged.
    size : int
        Number of transformations applied to every image (obtained from the input generator).
    """

    def identity():
        """
        Returns
        -------
        Transform
            Identity transformation that passes every input through unchanged.
        """
        def _gen(inputs):
            for d in inputs:
                yield d
        return Transform('Identity', _gen, 1)

    # def flip(axis):
    #     """TODO"""
    #     def _gen(inputs):
    #         for x,y,m_in in inputs:
    #             axis < x.ndim or _raise(ValueError())
    #             yield x, y, m_in
    #             yield np.flip(x,axis), np.flip(y,axis), None if m_in is None else np.flip(m_in,axis)
    #     return Transform('Flip (axis=%d)'%axis, _gen, 2)



## Raw data

class RawData(namedtuple('RawData',('generator','size','description'))):
    """:func:`collections.namedtuple` with three fields: `generator`, `size`, and `description`.

    Parameters
    ----------
    generator : function
        Function without arguments that returns a generator that yields tuples `(x,y,mask)`,
        where `x` is a source image (e.g., with low SNR) with `y` being the corresponding target image
        (e.g., with high SNR); `mask` can either be `None` or a boolean array that denotes which
        pixels are eligible to extracted in :func:`create_patches`. Note that `x`, `y`, and `mask`
        must all be of type :class:`numpy.ndarray` with the same shape.
    size : int
        Number of tuples that the `generator` will yield.
    description : str
        Textual description of the raw data.
    """

def get_tiff_pairs_from_folders(basepath,source_dirs,target_dir='GT',pattern='*.tif*'):
    """Get pairs of corresponding TIFF images read from folders.

    Two images correspond to each other if they have the same file name, but are located in different folders.

    Parameters
    ----------
    basepath : str
        Base folder that contains sub-folders with images.
    source_dirs : list or tuple
        List of folder names relative to `basepath` that contain the source images (e.g., with low SNR).
    target_dir : str
        Folder name relative to `basepath` that contains the target images (e.g., with high SNR).
    pattern : str
        Glob-style pattern to match the desired TIFF images.

    Returns
    -------
    RawData
        :obj:`RawData` object, whose `generator` is used to yield all matching TIFF pairs.
        The generator will return a tuple `(x,y,mask)`, where `x` is from
        `source_dirs` and `y` is the corresponding image from the `target_dir`; `mask` is
        set to `None`.

    Raises
    ------
    FileNotFoundError
        If an image found in `target_dir` does not exist in all `source_dirs`.
    ValueError
        If corresponding images do not have the same size (raised by returned :func:`RawData.generator`).

    Example
    --------
    >>> !tree data
    data
    ├── GT
    │   ├── imageA.tif
    │   └── imageB.tif
    ├── source1
    │   ├── imageA.tif
    │   └── imageB.tif
    └── source2
        ├── imageA.tif
        └── imageB.tif

    >>> data = get_tiff_pairs_from_folders(basepath='data', source_dirs=['source1','source2'], target_dir='GT')
    >>> n_images = data.size
    >>> for source_x, target_y, mask in data.generator():
    ...     pass
    """

    p = Path(basepath)
    image_names = [f.name for f in (p/target_dir).glob(pattern)]
    len(image_names) > 0 or _raise(FileNotFoundError("'target_dir' doesn't exist or didn't find any images in it."))
    consume ((
        (p/s/n).exists() or _raise(FileNotFoundError(p/s/n))
        for s in source_dirs for n in image_names
    ))
    xy_name_pairs = [(p/source_dir/n, p/target_dir/n) for source_dir in source_dirs for n in image_names]
    n_images = len(xy_name_pairs)
    description = '{p}: target=\'{o}\' sources={s}'.format(p=basepath,s=list(source_dirs),o=target_dir)

    def _gen():
        for fx, fy in xy_name_pairs:
            x, y = imread(str(fx)), imread(str(fy))
            # x,y = x[:,256:-256,256:-256],y[:,256:-256,256:-256] #tmp
            x.shape == y.shape or _raise(ValueError())
            yield x, y, None

    return RawData(_gen, n_images, description)



## Patch filter

def no_background_patches(threshold=0.4, percentile=99.9):
    """Returns a patch filter to be used by :func:`create_patches` to determine for each image pair which patches
    are eligible for sampling. The purpose is to only sample patches from "interesting" regions of the raw image that
    actually contain some non-background signal. To that end, a maximum filter is applied to the target image
    to find the largest values in a region.

    Parameters
    ----------
    threshold : float, optional
        Scalar threshold between 0 and 1 that will be multiplied with the (outlier-robust)
        maximum of the image (see `percentile` below) to denote a lower bound.
        Only patches with a maximum value above this lower bound are eligible to be sampled.
    percentile : float, optional
        Percentile value to denote the (outlier-robust) maximum of an image, i.e. should be close 100.

    Returns
    -------
    function
        Function that takes an image pair `(y,x)` and the patch size as arguments and
        returns a binary mask of the same size as the image (to denote the locations
        eligible for sampling for :func:`create_patches`). At least one pixel of the
        binary mask must be ``True``, otherwise there are no patches to sample.

    Raises
    ------
    ValueError
        Illegal arguments.
    """

    (np.isscalar(percentile) and 0 <= percentile <= 100) or _raise(ValueError())
    (np.isscalar(threshold)  and 0 <= threshold  <=   1) or _raise(ValueError())

    from scipy.ndimage.filters import maximum_filter
    def _filter(datas, patch_size, dtype=np.float32):
        image = datas[0]
        if dtype is not None:
            image = image.astype(dtype)
        # make max filter patch_size smaller to avoid only few non-bg pixel close to image border
        patch_size = [(p//2 if p>1 else p) for p in patch_size]
        filtered = maximum_filter(image, patch_size, mode='constant')
        return filtered > threshold * np.percentile(image,percentile)
    return _filter



## Sample patches

def sample_patches_from_multiple_stacks(datas, patch_size, n_samples, datas_mask=None, patch_filter=None, verbose=False):
    """ sample matching patches of size `patch_size` from all arrays in `datas` """

    # TODO: some of these checks are already required in 'create_patches'
    len(patch_size)==datas[0].ndim or _raise(ValueError())

    if not all(( a.shape == datas[0].shape for a in datas )):
        raise ValueError("all input shapes must be the same: %s" % (" / ".join(str(a.shape) for a in datas)))

    if not all(( 0 < s <= d for s,d in zip(patch_size,datas[0].shape) )):
        raise ValueError("patch_size %s negative or larger than data shape %s along some dimensions" % (str(patch_size), str(datas[0].shape)))

    if patch_filter is None:
        patch_mask = np.ones(datas[0].shape,dtype=np.bool)
    else:
        patch_mask = patch_filter(datas, patch_size)

    if datas_mask is not None:
        # FIXME: Test this
        import warnings
        warnings.warn('Using pixel masks for raw/transformed images not tested.')
        datas_mask.shape == datas[0].shape or _raise(ValueError())
        datas_mask.dtype == np.bool or _raise(ValueError())
        from scipy.ndimage.filters import minimum_filter
        patch_mask &= minimum_filter(datas_mask, patch_size, mode='constant', cval=False)

    # get the valid indices

    border_slices = tuple([slice(s // 2, d - s + s // 2 + 1) for s, d in zip(patch_size, datas[0].shape)])
    valid_inds = np.where(patch_mask[border_slices])

    if len(valid_inds[0]) == 0:
        raise ValueError("'patch_filter' didn't return any region to sample from")

    valid_inds = [v + s.start for s, v in zip(border_slices, valid_inds)]

    # sample
    sample_inds = np.random.choice(len(valid_inds[0]), n_samples, replace=len(valid_inds[0])<n_samples)

    rand_inds = [v[sample_inds] for v in valid_inds]

    # res = [np.stack([data[r[0] - patch_size[0] // 2:r[0] + patch_size[0] - patch_size[0] // 2,
    #                  r[1] - patch_size[1] // 2:r[1] + patch_size[1] - patch_size[1] // 2,
    #                  r[2] - patch_size[2] // 2:r[2] + patch_size[2] - patch_size[2] // 2,
    #                  ] for r in zip(*rand_inds)]) for data in datas]

    # FIXME: Test this
    res = [np.stack([data[tuple(slice(_r-(_p//2),_r+_p-(_p//2)) for _r,_p in zip(r,patch_size))] for r in zip(*rand_inds)]) for data in datas]

    return res



## Crate training data

def _valid_low_high_percentiles(ps):
    return isinstance(ps,(list,tuple,np.ndarray)) and len(ps)==2 and all(map(np.isscalar,ps)) and (0<=ps[0]<ps[1]<=100)

def _memory_check(n_required_memory_bytes, thresh_free_frac=0.5, thresh_abs_bytes=1024*1024**2):
    try:
        # raise ImportError
        import psutil
        mem = psutil.virtual_memory()
        mem_frac = n_required_memory_bytes / mem.available
        if mem_frac > 1:
            raise(MemoryError('Not enough available memory.'))
        elif mem_frac > thresh_free_frac:
            print('Warning: will use at least %.0f MB (%.1f%%) of available memory.\n' % (n_required_memory_bytes/1024**2,100*mem_frac), file=sys.stderr, flush=True)
    except ImportError:
        if n_required_memory_bytes > thresh_abs_bytes:
            print('Warning: will use at least %.0f MB of memory.\n' % (n_required_memory_bytes/1024**2), file=sys.stderr, flush=True)

def sample_percentiles(pmin=(1,3), pmax=(99.5,99.9)):
    """Sample percentile values from a uniform distribution.

    Parameters
    ----------
    pmin : tuple
        Tuple of two values that denotes the interval for sampling low percentiles.
    pmax : tuple
        Tuple of two values that denotes the interval for sampling high percentiles.

    Returns
    -------
    function
        Function without arguments that returns `(pl,ph)`, where `pl` (`ph`) is a sampled low (high) percentile.

    Raises
    ------
    ValueError
        Illegal arguments.
    """
    _valid_low_high_percentiles(pmin) or _raise(ValueError(pmin))
    _valid_low_high_percentiles(pmax) or _raise(ValueError(pmax))
    pmin[1] < pmax[0] or _raise(ValueError())
    return lambda: (np.random.uniform(*pmin), np.random.uniform(*pmax))


def norm_percentiles(percentiles=sample_percentiles(), relu_last=False):
    """Normalize extracted patches based on percentiles from corresponding raw image.

    Parameters
    ----------
    percentiles : tuple, optional
        A tuple (`pmin`, `pmax`) or a function that returns such a tuple, where the extracted patches
        are (affinely) normalized in such that a value of 0 (1) corresponds to the `pmin`-th (`pmax`-th) percentile
        of the raw image (default: :func:`sample_percentiles`).
    relu_last : bool, optional
        Flag to indicate whether the last activation of the CARE network is/will be using
        a ReLU activation function (default: ``False``)

    Return
    ------
    function
        Function that does percentile-based normalization to be used in :func:`create_patches`.

    Raises
    ------
    ValueError
        Illegal arguments.

    Todo
    ----
    ``relu_last`` flag problematic/inelegant.

    """
    if callable(percentiles):
        _tmp = percentiles()
        _valid_low_high_percentiles(_tmp) or _raise(ValueError(_tmp))
        get_percentiles = percentiles
    else:
        _valid_low_high_percentiles(percentiles) or _raise(ValueError(percentiles))
        get_percentiles = lambda: percentiles

    def _normalize(patches_x,patches_y, x,y,mask,channel):
        pmins, pmaxs = zip(*(get_percentiles() for _ in patches_x))
        percentile_axes = None if channel is None else tuple((d for d in range(x.ndim) if d != channel))
        _perc = lambda a,p: np.percentile(a,p,axis=percentile_axes,keepdims=True)
        patches_x_norm = normalize_mi_ma(patches_x, _perc(x,pmins), _perc(x,pmaxs))
        if relu_last:
            pmins = np.zeros_like(pmins)
        patches_y_norm = normalize_mi_ma(patches_y, _perc(y,pmins), _perc(y,pmaxs))
        return patches_x_norm, patches_y_norm

    return _normalize




def create_patches(
        raw_data,
        patch_size,
        n_patches_per_image,
        transforms = None,
        patch_filter = no_background_patches(),
        normalization = norm_percentiles(),
        channel = None,
        shuffle = True,
        verbose = True,
    ):
    """Create normalized training data to be used for neural network training.

    Parameters
    ----------
    raw_data : :class:`RawData`
        Object that yields matching pairs of raw images.
    patch_size : tuple
        Shape of the patches to be extraced from raw images.
        Must be compatible with the number of dimensions (2D/3D) and the shape of the raw images.
    n_patches_per_image : int
        Number of patches to be sampled/extracted from each raw image pair (after transformations, see below).
    transforms : list or tuple, optional
        List of :class:`Transform` objects that apply additional transformations to the raw images.
        This can be used to augment the set of raw images (e.g., by including rotations).
        Set to ``None`` to disable. Default: ``None``.
    patch_filter : function, optional
        Function to determine for each image pair which patches are eligible to be extracted
        (default: :func:`no_background_patches`). Set to ``None`` to disable.
    normalization : function, optional
        Function that takes arguments `(patches_x, patches_y, x, y, mask, channel)`, whose purpose is to
        normalize the patches (`patches_x`, `patches_y`) extracted from the associated raw images
        (`x`, `y`, with `mask`; see :class:`RawData`). Default: :func:`norm_percentiles`.
    channel : int, optional
        Index of channel for multi-channel images; set to ``None`` for single-channel images where
        raw images do not explicitly contain a channel dimension.
    shuffle : bool, optional
        Randomly shuffle all extracted patches.
    verbose : bool, optional
        Display overview of images, transforms, etc.

    Returns
    -------
    tuple(:class:`numpy.ndarray`, :class:`numpy.ndarray`)
        Returns a pair (`X`, `Y`) of arrays with the normalized extracted patches from all (transformed) raw images.
        `X` is the array of patches extracted from source images with `Y` being the array of corresponding target patches.
        The shape of `X` and `Y` is as follows: `(n_total_patches, n_channels, ...)`.
        For single-channel images (`channel` = ``None``), `n_channels` = 1.

    Raises
    ------
    ValueError
        Various reasons.

    Example
    -------
    >>> raw_data = get_tiff_pairs_from_folders(basepath='data', source_dirs=['source1','source2'], target_dir='GT')
    >>> X, Y = create_patches(raw_data, patch_size=(32,128,128), n_patches_per_image=16)

    Todo
    ----
    - Is :func:`create_patches` a good name?
    - Save created patches directly to disk using :class:`numpy.memmap` or similar?
      Would allow to work with large data that doesn't fit in memory.

    """
    ## images and transforms
    if transforms is None or len(transforms)==0:
        transforms = (Transform.identity(),)
    image_pairs, n_raw_images = raw_data.generator(), raw_data.size
    tf = Transform(*zip(*transforms)) # convert list of Transforms into Transform of lists
    image_pairs = compose(*tf.generator)(image_pairs) # combine all transformations with raw images as input
    n_transforms = np.prod(tf.size)
    n_images = n_raw_images * n_transforms
    n_patches = n_images * n_patches_per_image
    n_required_memory_bytes = 2 * n_patches*np.prod(patch_size) * 4

    ## memory check
    _memory_check(n_required_memory_bytes)

    ## summary
    if verbose:
        print('='*66)
        print('%5d raw images x %4d transformations   = %5d images' % (n_raw_images,n_transforms,n_images))
        print('%5d images     x %4d patches per image = %5d patches in total' % (n_images,n_patches_per_image,n_patches))
        print('='*66)
        print('Input data:')
        print(raw_data.description)
        print('='*66)
        print('Transformations:')
        for t in transforms:
            print('{t.size} x {t.name}'.format(t=t))
        print('='*66)
        print(flush=True)

    ## sample patches from each pair of transformed raw images
    X = np.empty((n_patches,)+tuple(patch_size),dtype=np.float32)
    Y = np.empty_like(X)

    for i, (x,y,mask) in tqdm(enumerate(image_pairs),total=n_images):
        # checks
        x.shape == y.shape or _raise(ValueError())
        mask is None or mask.shape == x.shape or _raise(ValueError())
        (channel is None or (isinstance(channel,int) and 0<=channel<x.ndim)) or _raise(ValueError())
        channel is None or patch_size[channel]==x.shape[channel] or _raise(ValueError('extracted patches must contain all channels.'))

        _Y,_X = sample_patches_from_multiple_stacks((y,x), patch_size, n_patches_per_image, mask, patch_filter)

        s = slice(i*n_patches_per_image,(i+1)*n_patches_per_image)
        X[s], Y[s] = normalization(_X,_Y, x,y,mask,channel)

    if shuffle:
        shuffle_inplace(X,Y)

    if channel is None:
        X = np.expand_dims(X,1)
        Y = np.expand_dims(Y,1)
    else:
        X = np.moveaxis(X, 1+channel, 1)
        Y = np.moveaxis(Y, 1+channel, 1)

    return X,Y



def anisotropic_distortions(
        subsample,
        psf,
        z              = 0,
        channel        = None,
        poisson_noise  = False,
        gauss_sigma    = 0,
        crop_threshold = 0.2,
    ):
    """Simulate anisotropic distortions along z.

    Modify x and y dimensions to mimic the distortions that occur due to
    low resolution along z. Note that the modified image is finally upscaled
    to obtain the same resolution as the unmodified input image.

    Parameters
    ----------
    subsample : list
        List of subsampling factors to apply tothe image.
        Each factor should be a tuple of subsampling factors of the x and y dimensions
        (in the order as they appear in the raw image dimensions).
    psf : :class:`numpy.ndarray` or None
        Point spread function (PSF) that is supposed to mimic blurring
        of the microscope due to reduced axial resolution.
        Must be compatible with the number of dimensions (2D/3D) and the shape of the raw images.
    z : int
        Index of z dimension.
    channel : int, optional
        Index of channel for multi-channel images; set to ``None`` for single-channel images where
        raw images do not explicitly contain a channel dimension.
    poisson_noise : bool
        Flag to indicate whether Poisson noise should be added to the image.
    gauss_sigma : int
        Standard deviation of white Gaussian noise to be added to the image (after Poisson).
    crop_threshold : float
        The subsample factors must evenly divide the raw image dimensions to prevent
        potential image misalignement. If this is not the case the subsample factors are
        modified and the raw image will be cropped up to a fraction indiced by `crop_threshold`.

    Returns
    -------
    Transform
        Returns a :class:`Transform` object to be used with :func:`create_patches` to
        create training data for an isotropic reconstruction CARE network.

    Raises
    ------
    ValueError
        Various reasons.

    """
    zoom_order = 1

    isinstance(subsample,(tuple,list)) or _raise(ValueError('subsample must be list of tuples'))
    subsample_list = subsample

    0 < crop_threshold < 1 or _raise(ValueError())

    channel is None or isinstance(channel,int) or _raise(ValueError())
    isinstance(z,int) or _raise(ValueError())
    psf is None or isinstance(psf,np.ndarray) or _raise(ValueError())





    def _normalize_data(data,undo=False):
        """Move channel and z to front of image."""
        if undo:
            if channel is None:
                return np.moveaxis(data[0],0,z)
            else:
                return np.moveaxis(data,[0,1],[channel,z])
        else:
            if channel is None:
                return np.moveaxis(np.expand_dims(data,-1), [-1,z],[0,1])
            else:
                return np.moveaxis(data,[channel,z],[0,1])

    def _scale_down_up(data,subsample):
        from scipy.ndimage.interpolation import zoom
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return zoom(zoom(data, (1,1,1./subsample[0],1./subsample[1]), order=0),
                                   (1,1,   subsample[0],   subsample[1]), order=zoom_order)

    # def _subsample_shape(shape):
    #     """
    #     returns the shape of the result when down and upsampling an array of shape shape
    #     """
    #     from scipy.version import full_version as scipy_version
    #     from distutils.version import LooseVersion
    #     if LooseVersion(scipy_version) >= LooseVersion('0.13.0'):
    #         disc = lambda v: int(round(v))
    #     else:
    #         disc = lambda v: int(v)
    #     return shape[:2] + tuple( disc(disc(n/s)*s) for s,n in zip(subsample,shape[2:]) )

    # def _resize_to_shape(x, shape, mode='constant'):
    #     diff = np.array(shape) - np.array(x.shape)
    #     # first shrink
    #     slices = tuple(slice(d//2,-(d-d//2)) if d>0 else slice(None,None) for d in -diff)
    #     x = x[slices]
    #     if x.shape == shape:
    #         return x
    #     # then pad
    #     return np.pad(x, [(  int(np.ceil(d/2.)),
    #                        d-int(np.ceil(d/2.))) if d>0 else (0,0) for d in diff], mode=mode)

    # def _crop_xy_border(x,b=5):
    #     return x[...,b:-b,b:-b] if b > 0 else x

    def adjust_subsample(d,s,c):
        """length d, subsample s, tolerated crop loss fraction c"""
        from fractions import Fraction

        def crop_size(n_digits,frac):
            _s = round(s,n_digits)
            _div = frac.denominator
            s_multiple_max = np.floor(d/_s)
            s_multiple = (s_multiple_max//_div)*_div
            # print(n_digits, _s,_div,s_multiple)
            size = s_multiple * _s
            assert np.allclose(size,round(size))
            return size

        def decimals(v,n_digits=None):
            if n_digits is not None:
                v = round(v,n_digits)
            s = str(v)
            assert '.' in s
            decimals = s[1+s.find('.'):]
            return int(decimals), len(decimals)

        s = float(s)
        dec, n_digits = decimals(s)
        frac = Fraction(dec,10**n_digits)
        # a multiple of s that is also an integer number must be
        # divisible by the denominator of the fraction that represents the decimal points

        # round off decimals points if needed
        while n_digits > 0 and (d-crop_size(n_digits,frac))/d > c:
            n_digits -= 1
            frac = Fraction(decimals(s,n_digits)[0], 10**n_digits)

        size = crop_size(n_digits,frac)
        if size == 0 or (d-size)/d > c:
            raise ValueError("subsample factor %g too large (crop_threshold=%g)" % (s,c))

        return round(s,n_digits), int(round(crop_size(n_digits,frac)))


    def _make_divisible_by_subsample(x,sizes):
        def _split_slice(v):
            return slice(None) if v==0 else slice(v//2,-(v-v//2))
        slices = (slice(None),slice(None)) + tuple(
            # # it's late... there must be a (much) simpler way to do this!
            # _split_slice(d-next(int(np.round(s*i)) for i in range(int(np.floor(d/s)),1,-1) if np.allclose(np.round(i*s),i*s)))
            _split_slice(d-sz)
            for sz,d in zip(sizes,x.shape[2:])
        )
        return x[slices]


    def _generator(inputs):
        for img,y,mask in inputs:

            if not (y is None or np.all(img==y)):
                warnings.warn('ignoring y.')
            if mask is not None:
                warnings.warn('ignoring mask.')
            del y, mask

            # tmp
            # print(img.shape)
            img = img[...,:256,:256]

            _img, _x = img, img.astype(np.float32, copy=False)

            if psf is not None:
                _x.ndim == psf.ndim or _raise(ValueError('image and psf must have the same number of dimensions.'))
                # print("blurring with psf")
                from scipy.signal import fftconvolve
                _x = fftconvolve(_x, psf, mode='same')


            for _subsample in subsample_list:
                if not isinstance(_subsample,(tuple,list)):
                    _subsample = (1, _subsample)
                assert len(_subsample) == 2

                # start with non-subsampled images
                img, x = _img, _x

                if bool(poisson_noise):
                    # print("apply poisson noise")
                    x = np.random.poisson(np.maximum(0,x).astype(np.int)).astype(np.float32)

                if gauss_sigma > 0:
                    # print("adding gaussian noise with sigma = ", gauss_sigma)
                    noise = np.random.normal(0,gauss_sigma,size=x.shape)
                    x = np.maximum(0,x+noise)

                if any(s != 1 for s in _subsample):
                    # print("down and upsampling by factors %s" % str(_subsample))
                    img = _normalize_data(img)
                    x   = _normalize_data(x)

                    subsample, subsample_sizes = zip(*[
                        adjust_subsample(d,s,crop_threshold) for s,d in zip(_subsample,x.shape[2:])
                    ])
                    # print(subsample, subsample_sizes)
                    if _subsample != subsample:
                        warnings.warn('changing subsample from %s to %s' % (str(_subsample),str(subsample)))

                    img = _make_divisible_by_subsample(img,subsample_sizes)
                    x   = _make_divisible_by_subsample(x,  subsample_sizes)
                    x   = _scale_down_up(x,subsample)

                    assert x.shape == img.shape, (x.shape, img.shape)

                    img = _normalize_data(img,undo=True)
                    x   = _normalize_data(x,  undo=True)

                    # why do I need _subsample_shape if I can just call u.shape instead???
                    # assert u.shape == _subsample_shape(x_norm.shape)

                    # not clear why _resize_to_shape does the right thing, i.e. align both images as best as possible
                    # x_norm_pad = _resize_to_shape(x_norm,u.shape)
                    # # from skimage.feature import register_translation
                    # # shifts = register_translation(u,x_norm_pad)[0]
                    # # assert np.all(shifts==0), shifts

                    # crop border to get rid of potential upsampling artifacts
                    # u, x_norm_pad = _crop_xy_border(u), _crop_xy_border(x_norm_pad)

                yield x, img, None


    return Transform('Anisotropic distortions', _generator, len(subsample_list))
