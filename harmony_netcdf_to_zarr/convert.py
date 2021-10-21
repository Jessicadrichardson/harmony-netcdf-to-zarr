import collections
import os
import sys
import multiprocessing
from multiprocessing import Semaphore
from typing import Union

import s3fs
import numpy as np
import zarr
from netCDF4 import Dataset

region = os.environ.get('AWS_DEFAULT_REGION') or 'us-west-2'


def make_localstack_s3fs():
    host = os.environ.get('LOCALSTACK_HOST') or 'host.docker.internal'
    return s3fs.S3FileSystem(
        use_ssl=False,
        key='ACCESS_KEY',
        secret='SECRET_KEY',
        client_kwargs=dict(
            region_name=region,
            endpoint_url='http://%s:4572' % (host)))


def make_s3fs():
    return s3fs.S3FileSystem(client_kwargs=dict(region_name=region))


def netcdf_to_zarr(src, dst):
    """
    Convert the NetCDF file at src to the zarr file at dst, preserving data, metadata, and
    group hierarchy


    Parameters
    ----------
    src : string | netCDF4.Dataset
        The file to convert, either a location on disk or an already-opened dataset
    dst : string | collections.MutableMapping
        The output zarr file.  Either a location on disk into which a zarr.DirectoryStore
        will be written or a MutableMapping into which zarr data can be written.
    """
    managed_resources = []
    try:
        # Allow passing in a path to a store or a file
        if isinstance(src, str):
            src = Dataset(src, 'r')
            managed_resources.append(src)

        if isinstance(dst, str):
            dst = zarr.DirectoryStore(dst)
            managed_resources.append(dst)

        src.set_auto_mask(False)
        src.set_auto_scale(True)
        __copy_group(src, zarr.group(dst, overwrite=True))
        zarr.convenience.consolidate_metadata(dst)

    finally:
        for resource in managed_resources:
            try:
                resource.close()
            except BaseException:
                pass


def scale_attribute(src, attr, scale_factor, add_offset):
    """
    Scales an unscaled NetCDF attribute


    Parameters
    ----------
    src : netCDF4.Variable
        the source variable to copy
    attr : collections.Sequence | numpy.ndarray | number
        the NetCDF variable attribute that needs to be scaled
    scale_factor : number
        the number used to multiply unscaled data
    add_offset : number
        the number added to unscaled data after multiplied by scale_factor

    Returns
    -------
    list | number
        the scaled data; either a list of floats or a float scalar
    """
    scale_fn = lambda x: float(x * scale_factor + add_offset)
    unscaled = getattr(src, attr)
    if isinstance(unscaled, collections.Sequence) or isinstance(unscaled, np.ndarray):
        return [scale_fn(u) for u in unscaled]
    else:
        return scale_fn(unscaled)


def regenerate_chunks(shape, chunks):
    """
    Regenerate new chunks based on given zarr chunks

    Parameters
    ----------
    shape : list/tuple
        the zarr shape
    chunks : list/tuple
        the original zarr chunks

    Returns
    -------
    list
        the regenerated new zarr chunks
    """
    # regenerate new chunks
    # NOTE currently make each chunk dimension to be its multiplier closest to 3000
    #   with a max chunksize of 3000
    new_chunks = map(
        lambda x: min(x[0], int(3000 / x[1]) * x[1] if x[1] < 3000 else 3000),
        zip(shape, chunks),
    )
    new_chunks = type(chunks)(list(new_chunks))

    # return new chunks
    return new_chunks


def suggest_chunksize(shape: Union[tuple, list],
                      datatype: str,
                      compression_ratio: float = 7.2,
                      compressed_chunksize: Union[int, str] = '10 Mi'):
    """
    Suggest chunk size by trying to balance between all dimensions

    Parameters
    ----------
    shape : list/tuple
        the zarr shape
    datatype: str
        the zarr data type
    compression_ratio: str
        expected compression ratio for each chunk
        default to 7.2 which is the compression ratio
        from a chunk size of (3000, 3000) with double precision
        compressed to 10 Mi
    compressed_chunksize: string
        expected chunk size after compression
        If it's a string, assuming it follows NIST standard for binary prefix
        (https://physics.nist.gov/cuu/Units/binary.html)
        space is optional between number and unit

    Returns
    -------
    list/tuple
        the regenerated new zarr chunks
    """
    # suggest chunk size by trying to balance between all dimensions
    suggested_chunksize = None

    # return new chunks
    return suggested_chunksize


def __copy_variable(src, dst_group, name, sema=Semaphore(20)):
    """
    Copies the variable from the NetCDF src variable into the Zarr group dst_group, giving
    it the provided name

    Parameters
    ----------
    src : netCDF4.Variable
        the source variable to copy
    dst_group : zarr.hierarchy.Group
        the group into which to copy the variable
    name : string
        the name of the variable in the destination group
    sema: multiprocessing.synchronize.Semaphore
        Semaphore used to limit concurrent processes
        NOTE: the default value 20 is empirical

    Returns
    -------
    zarr.core.Array
        the copied variable
    """
    # acquire Semaphore
    sema.acquire()

    # connect to s3
    if os.environ.get('USE_LOCALSTACK') == 'true':
        s3 = make_localstack_s3fs()
    else:
        s3 = make_s3fs()
    group_name = os.path.join(dst_group.store.root, dst_group.path)
    dst = s3.get_mapper(root=group_name, check=False, create=True)
    dst_group = zarr.group(dst)

    # create zarr group/dataset
    chunks = src.chunking()
    if chunks == 'contiguous' or chunks is None:
        chunks = src.shape
    if not chunks and len(src.dimensions) == 0:
        # Treat a 0-dimensional NetCDF variable as a zarr group
        dst = dst_group.create_group(name)
    else:
        dtype = src.dtype
        dtype = src.scale_factor.dtype if hasattr(src, 'scale_factor') else dtype
        dtype = src.add_offset.dtype if hasattr(src, 'add_offset') else dtype
        new_chunks = regenerate_chunks(src.shape, chunks)
        dst = dst_group.create_dataset(name,
                                       data=src,
                                       shape=src.shape,
                                       chunks=tuple(new_chunks),
                                       dtype=dtype)

    # Apply scale factor and offset to attributes that are not automatically scaled by NetCDF
    scaled = {}
    scale_factor = getattr(src, 'scale_factor', 1.0)
    add_offset = getattr(src, 'add_offset', 0.0)
    if scale_factor != 1.0 or add_offset != 0.0:
        unscaled_attributes = ['valid_range', 'valid_min', 'valid_max', '_FillValue', 'missing_value']
        present_attributes = [attr for attr in unscaled_attributes if hasattr(src, attr)]
        scaled = {attr: scale_attribute(src, attr, scale_factor, add_offset) for attr in present_attributes}

    # xarray requires the _ARRAY_DIMENSIONS metadata to know how to label axes
    __copy_attrs(src, dst, scaled, _ARRAY_DIMENSIONS=list(src.dimensions))

    # release Semaphore
    sema.release()

    return dst


def __copy_attrs(src, dst, scaled={}, **kwargs):
    """
    Copies all attributes from the source group or variable into the destination group or variable.
    Converts netCDF4 variable values from their native type (typically Numpy dtypes) into
    JSON-serializable values that Zarr can store

    Parameters
    ----------
    src : netCDF4.Group | netCDF4.Variable
        The source from which to copy attributes
    dst : zarr.hierarchy.Group | zarr.core.Array
        The destination into which to copy attributes.
    **kwargs : dict
        Additional attributes to add to the destination
    """
    attrs = {key: __netcdf_attr_to_python(getattr(src, key)) for key in src.ncattrs()}
    attrs.update(kwargs)
    attrs.update(scaled)
    attrs.pop('scale_factor', None)
    attrs.pop('add_offset', None)
    dst.attrs.put(attrs)


def __copy_group(src, dst):
    """
    Recursively copies the source netCDF4 group into the destination Zarr group, along with
    all sub-groups, variables, and attributes
    NOTE: the variables will be copied in parallel processes via multiprocessing;
          'fork' is used as the start-method because OSX/Windows is using 'spawn' by default,
          which will introduce overhead and difficulties pickling data objects (and to the test);
          Semaphore is used to limit the number of concurrent processes,
          which is set to double the number of cpu-s found on the host

    Parameters
    ----------
    src : netCDF4.Group
        the NetCDF group to copy from
    dst : zarr.hierarchy.Group
        the existing Zarr group to copy into
    """
    __copy_attrs(src, dst)

    for name, item in src.groups.items():
        __copy_group(item, dst.create_group(name.split('/').pop()))

    procs = []
    fork_ctx = multiprocessing.get_context('fork')
    sema = Semaphore(multiprocessing.cpu_count() * 2)
    for name, item in src.variables.items():
        proc = fork_ctx.Process(target=__copy_variable, args=(item, dst, name, sema))
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join()


def __netcdf_attr_to_python(val):
    """
    Given an attribute value read from a NetCDF file (typically a numpy type),
    returns the value as a Python primitive type, e.g. np.integer -> int.

    Returns the value unaltered if it does not need conversion or is unrecognized

    Parameters
    ----------
    val : any
        An attribute value read from a NetCDF file

    Returns
    -------
    any
        The converted value
    """
    if isinstance(val, np.integer):
        return int(val)
    elif isinstance(val, np.floating):
        return float(val)
    elif isinstance(val, np.ndarray):
        return [__netcdf_attr_to_python(v) for v in val.tolist()]
    elif isinstance(val, bytes):
        # Assumes bytes are UTF-8 strings.  This holds for attributes.
        return val.decode("utf-8")
    return val


if __name__ == '__main__':
    netcdf_to_zarr(sys.argv[1], sys.argv[2])
