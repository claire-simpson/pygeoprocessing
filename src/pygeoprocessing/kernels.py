import logging
import math

import numpy
import pygeoprocessing
from osgeo import gdal
from osgeo import osr

FLOAT32_NODATA = numpy.finfo(numpy.float32).min
LOGGER = logging.getLogger(__name__)


def dichotomous_kernel(target_kernel_path, max_distance):
    def _dichotomy(distance_from_center):
        return numpy.array(
            distance_from_center <= max_distance, dtype=numpy.float32)

    _create_distance_based_kernel(
        target_kernel_path, _dichotomy, max_distance, normalize=False)


def density_decay_kernel(target_kernel_path, max_distance):
    def _density(distance_from_center):
        density = numpy.zeros(
            distance_from_center.shape, dtype=numpy.float32)
        pixels_in_radius = (distance_from_center <= max_distance)
        density[pixels_in_radius] = (
            0.75 * (1 - (distance_from_center[
                pixels_in_radius] / max_distance) ** 2))
        return density

    _create_distance_based_kernel(
        target_kernel_path, _density, max_distance, normalize=False)


def exponential_decay_kernel(target_kernel_path, max_distance):
    def _exp_decay(distance_from_center):
        kernel = numpy.where(
            distance_from_center > max_distance, 0.0,
            numpy.exp(-distance_from_center / max_distance))
        return kernel

    _create_distance_based_kernel(
        target_kernel_path, _exp_decay, max_distance, normalize=True)


def linear_decay_kernel(target_kernel_path, max_distance):
    def _linear_decay(distance_from_center):
        return numpy.where(
            distance_from_center > max_distance, 0.0,
            (max_distance - distance_from_center) / max_distance)

    _create_distance_based_kernel(
        target_kernel_path, _linear_decay, max_distance, normalize=True)


def gaussian_decay_kernel(target_kernel_path, sigma, n_std_dev):
    max_distance = sigma * n_std_dev

    def _gaussian_decay(distance_from_center):
        kernel = numpy.where(
            distance_from_center > max_distance, 0.0,
            (1 / (2.0 * numpy.pi * sigma ** 2) *
             numpy.exp(-distance_from_center**2 / (2 * sigma ** 2))))
        return kernel

    _create_distance_based_kernel(
        target_kernel_path, _gaussian_decay, max_distance, normalize=True)


def _create_distance_based_kernel(
        target_kernel_path, function, max_distance, normalize=False):
    """
    Create a kernel raster based on pixel distance from the centerpoint.

    Args:
        target_kernel_path (string): The path to where the target kernel should
            be written on disk.  If this file does not have the suffix
            ``.tif``, it will be added to the filepath.
        function (callable): A python callable that takes as input a
            2D numpy array and returns a 2D numpy array.  The input array will
            contain float32 distances to the centerpoint pixel of the kernel.
        max_distance (float): The maximum distance of kernel values from
            the center point.  Values outside of this distance will be set to
            ``0.0``.
        normalize=False (bool): Whether to normalize the resulting kernel.

    Returns:
        ``None``
    """
    pixel_radius = math.ceil(max_distance)
    kernel_size = pixel_radius * 2 + 1  # allow for a center pixel
    driver = gdal.GetDriverByName('GTiff')
    kernel_dataset = driver.Create(
        target_kernel_path.encode('utf-8'), kernel_size, kernel_size, 1,
        gdal.GDT_Float32, options=[
            'BIGTIFF=IF_SAFER', 'TILED=YES', 'BLOCKXSIZE=256',
            'BLOCKYSIZE=256'])

    # Make some kind of geotransform, it doesn't matter what but
    # will make GIS libraries behave better if it's all defined
    kernel_dataset.SetGeoTransform([0, 1, 0, 0, 0, -1])
    srs = osr.SpatialReference()
    srs.SetWellKnownGeogCS('WGS84')
    kernel_dataset.SetProjection(srs.ExportToWkt())

    kernel_band = kernel_dataset.GetRasterBand(1)
    kernel_nodata = FLOAT32_NODATA
    kernel_band.SetNoDataValue(kernel_nodata)

    kernel_band = None
    kernel_dataset = None

    kernel_raster = gdal.OpenEx(target_kernel_path, gdal.GA_Update)
    kernel_band = kernel_raster.GetRasterBand(1)
    band_x_size = kernel_band.XSize
    band_y_size = kernel_band.YSize
    running_sum = 0
    for block_data in pygeoprocessing.iterblocks(
            (target_kernel_path, 1), offset_only=True):
        array_xmin = block_data['xoff'] - pixel_radius
        array_xmax = min(
            array_xmin + block_data['win_xsize'],
            band_x_size - pixel_radius)
        array_ymin = block_data['yoff'] - pixel_radius
        array_ymax = min(
            array_ymin + block_data['win_ysize'],
            band_y_size - pixel_radius)

        pixel_dist_from_center = numpy.hypot(
            *numpy.mgrid[
                array_ymin:array_ymax,
                array_xmin:array_xmax])
        kernel = function(pixel_dist_from_center)
        running_sum += kernel.sum()
        kernel_band.WriteArray(
            kernel,
            yoff=block_data['yoff'],
            xoff=block_data['xoff'])

    kernel_raster.FlushCache()
    kernel_band = None
    kernel_raster = None

    if normalize:
        kernel_raster = gdal.OpenEx(target_kernel_path, gdal.GA_Update)
        kernel_band = kernel_raster.GetRasterBand(1)
        for block_data, kernel_block in pygeoprocessing.iterblocks(
                (target_kernel_path, 1)):
            # divide by sum to normalize
            kernel_block /= running_sum
            kernel_band.WriteArray(
                kernel_block, xoff=block_data['xoff'], yoff=block_data['yoff'])

        kernel_raster.FlushCache()
        kernel_band = None
        kernel_raster = None
