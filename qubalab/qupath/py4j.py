from ..images import ImageServer, PixelLength, PixelCalibration, ImageServerMetadata, ImageShape, ImageChannel
from ..images.servers import _validate_block, _resize

from dataclasses import astuple
from typing import List, Tuple, Iterable, Union
from py4j.java_gateway import JavaGateway, JavaObject, GatewayParameters

from imageio.v3 import imread
from imageio import volread
import numpy as np

from dask import array as da

from ..objects import ImageObject, to_geometry
from ..objects import types
import geojson
from geojson import Feature

from urllib.parse import urlparse, unquote
import warnings
import base64
import tempfile
import os

"""
Store default Gateway so it doesn't need to always be passed as a parameter
"""
_default_gateway: JavaGateway = None


class QuPathServer(ImageServer):

    def __init__(self, gateway: JavaGateway = None, server_obj: JavaObject = None,
                 pixel_access: str = 'base64', **kwargs):
        """_summary_
        Args:
            gateway (JavaGateway, optional): _description_. Defaults to None.
            server_obj (JavaObject, optional): _description_. Defaults to None.
            pixel_access (str, optional): Method of accessing pixel values.
                                          This can be 'base64', 'bytes' or 'tempfile'.
                                          Default is 'base64', which tends to be faster than 'bytes'.
                                          'tempfile' is the fastest, but requires writing files to disk.

        Raises:
            ValueError: _description_
        """
        super().__init__(**kwargs)
        self._pixel_access = pixel_access
        self._gateway = _gateway_or_default(gateway)
        # Get the current server if none is specified
        if server_obj is None:
            server_obj = get_current_image_data(gateway).getServer()
        self._server_obj = server_obj
        if self._gateway is None:
            raise ValueError('A JavaGateway is needed! See create_gateway() for details.')

    def _build_metadata(self) -> ImageServerMetadata:
        # Get what we can directly from the server
        server = self._server_obj
        downsamples = tuple([d for d in server.getPreferredDownsamples()])

        self._downsamples = downsamples  # Use the QuPath values directly

        shapes = tuple([ImageShape(x=level.getWidth(), y=level.getHeight(), c=server.nChannels(), z=server.nZSlices(),
                                   t=server.nTimepoints())
                        for level in server.getMetadata().getLevels()])

        dt = np.dtype(server.getPixelType().toString().lower())
        is_rgb = server.isRGB()

        meta = server.getMetadata()
        name = meta.getName()
        cal = server.getPixelCalibration()

        channels = tuple([ImageChannel(c.getName(), _unpack_color(c.getColor())) for c in meta.getChannels()])

        # Try to get the file path
        path = _find_server_file_path(server)
        if path is None:
            path = server.getPath()

        if cal.hasPixelSizeMicrons():
            if cal.getZSpacingMicrons():
                length_z = PixelLength.create_microns(cal.getZSpacingMicrons())
            else:
                length_z = PixelLength()
            pixel_cal = PixelCalibration(
                length_x=PixelLength.create_microns(cal.getPixelWidthMicrons()),
                length_y=PixelLength.create_microns(cal.getPixelHeightMicrons()),
                length_z=length_z
            )
        else:
            pixel_cal = PixelCalibration()

        return ImageServerMetadata(
            path=path,
            name=name,
            pixel_calibration=pixel_cal,
            shapes=shapes,
            dtype=dt,
            is_rgb=is_rgb,
            channels=channels
        )

    def read_block(self, level: int, block: Tuple[int, ...]) -> np.ndarray:
        _, x, y, width, height, z, t = astuple(_validate_block(block))

        gateway = self._gateway
        server = self._server_obj

        # TODO: Explore requesting directly in QuPath - this is awkward and could result in 
        # rounding problems
        if level < 0:
            level = len(self.downsamples) + level
        downsample = server.getDownsampleForResolution(level)

        request = gateway.jvm.qupath.lib.regions.RegionRequest.createInstance(
            server.getPath(), downsample,
            int(round(x * downsample)),
            int(round(y * downsample)),
            int(round(width * downsample)),
            int(round(height * downsample)),
            z,
            t)

        import time
        start_time = time.time()

        if self._pixel_access == 'tempfile':
            temp_path = tempfile.mkstemp(prefix='qubalab-', suffix='.tif')[1]
            gateway.entry_point.writeImageRegion(server, request, temp_path)
            if self.metadata.is_rgb or self.n_channels == 1:
                im = imread(temp_path)
            else:
                # We can just provide 2D images; using volread move to channels-last
                im = np.moveaxis(volread(temp_path), 0, -1)
            os.remove(temp_path)
        else:
            fmt = 'png' if self.is_rgb else "imagej tiff"
            fmt = "imagej tiff"
            if self._pixel_access == 'bytes':
                byte_array = gateway.entry_point.getImageBytes(server, request, fmt)
            else:
                im_base64 = gateway.entry_point.getImageBase64(server, request, fmt)
                byte_array = base64.b64decode(im_base64)

            if self.metadata.is_rgb or self.n_channels == 1:
                im = imread(byte_array)
            else:
                # We can just provide 2D images; using volread move to channels-last
                im = np.moveaxis(volread(byte_array), 0, -1)

        end_time = time.time()
        print(f'Read time: {end_time - start_time:.2f} seconds')

        if height != im.shape[0] or width != im.shape[1]:
            shape_before = im.shape
            im = _resize(im, (width, height), self.resize_method)
            warnings.warn(f'Block needs to be reshaped from {shape_before} to {im.shape}')

        return im


def _unpack_color(rgb: int) -> Tuple[float, float, float]:
    r = (rgb >> 16) & 255
    g = (rgb >> 8) & 255
    b = rgb & 255
    return r / 255.0, g / 255.0, b / 255.0


def _get_server_uris(server: JavaObject) -> Tuple[str]:
    """
    Get URIs for a java object representing an ImageServer.
    """
    return tuple(str(u) for u in server.getURIs())


def _find_server_file_path(server: JavaObject) -> str:
    """
    Try to get the file path for a java object representing an ImageServer.
    This can be useful to get direct access to an image file, rather than via QuPath.
    """
    uris = _get_server_uris(server)
    if len(uris) == 1:
        parsed = urlparse(uris[0])
        if parsed.scheme == 'file':
            return unquote(parsed.path)
    return None


def get_current_image_data(gateway: JavaGateway = None) -> JavaObject:
    """
    Get the current ImageData open in QuPath
    """
    return _gateway_or_default(gateway).entry_point.getQuPath().getImageData()


def create_gateway(auto_convert=True, auth_token=None, set_as_default=True, **kwargs) -> JavaGateway:
    """
    Create a new JavaGateway to communicate with QuPath.
    This requires also launching QuPath and activating Py4J from there first.
    """
    if auth_token is not None:
        params = GatewayParameters(auth_token=auth_token)
        gateway = JavaGateway(auto_convert=auto_convert, gateway_parameters=params, **kwargs)
    else:
        # from py4j.clientserver import ClientServer, JavaParameters, PythonParameters
        # gateway = ClientServer(
        #     java_parameters=JavaParameters(auto_convert=auto_convert),
        #     python_parameters=PythonParameters())
        gateway = JavaGateway(auto_convert=auto_convert, **kwargs)
    if set_as_default:
        set_default_gateway(gateway)
    return gateway


def set_default_gateway(gateway: JavaGateway = None):
    """
    Set the default JavaGateway to use if one is not otherwise specified.
    """
    global _default_gateway
    _default_gateway = gateway


def _gateway_or_default(*args, **kwargs):
    """
    Attempt to get a gateway, by
    * returning one of the input argument, if it's a gateway
    * returning the default gateway, if available
    * creating a new gateway using the **kwargs (if needed, and displaying a warning)
    """
    for arg in args:
        if isinstance(arg, JavaGateway):
            return arg
    gateway = _default_gateway
    if gateway is None:
        warnings.warn('Attempting to create new JavaGateway')
        gateway = create_gateway(**kwargs)
    return gateway


def _get_java_class_name(input: JavaObject) -> str:
    return str(input.getClass().getName())


def _get_java_image_data(input) -> JavaObject:
    """
    Get an ImageData from the input, if possible.
    """
    if input is None:
        return get_current_image_data()
    if isinstance(input, JavaGateway):
        return get_current_image_data(gateway=input)
    if isinstance(input, JavaObject):
        cls = _get_java_class_name(input)
        if cls == 'qupath.lib.images.ImageData':
            return input
    return None


def _get_java_hierarchy(input) -> JavaObject:
    """
    Get a PathObjectHierarchy from the input, if possible.
    """
    if isinstance(input, JavaObject):
        cls = _get_java_class_name(input)
        if cls == 'qupath.lib.objects.hierarchy.PathObjectHierarchy':
            return input
    image_data = _get_java_image_data(input)
    return None if image_data is None else image_data.getHierarchy()


def _get_java_server(input) -> JavaObject:
    """
    Get an ImageServer from the input, if possible.
    """
    # Could use this, but then we definitely need a gateway
    #    cls_server = gateway.jvm.Class.forName('qupath.lib.images.servers.ImageServer')
    if isinstance(input, JavaObject):
        for cls in input.getClass().getInterfaces():
            if str(cls.getName()) == 'qupath.lib.images.servers.ImageServer':
                return input
    image_data = _get_java_image_data(input)
    return None if image_data is None else image_data.getServer()


class ExtendedGeoJsonEncoder(geojson.GeoJSONEncoder):
    def default(self, o):
        import dataclasses
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


def add_objects(features: List[Feature], image_data: JavaObject = None, gateway: JavaGateway = None):
    if not features:
        return
    gateway = _gateway_or_default(gateway)
    if image_data is None:
        image_data = get_current_image_data(gateway=gateway)
    else:
        image_data = _get_java_image_data(image_data)
    if image_data is None:
        raise ValueError('Cannot find an ImageData')
    # import json
    # json_str = json.dumps(features, cls=ExtendedGeoJsonEncoder, allow_nan=True, indent=2)
    json_str = geojson.dumps(features, allow_nan=True, indent=2)
    path_objects = gateway.entry_point.toPathObjects(json_str)
    image_data.getHierarchy().addObjects(path_objects);


def add_object(features: Feature, image_data: JavaObject = None, gateway: JavaGateway = None):
    add_objects(list(features), image_data=image_data, gateway=gateway)


def delete_all_objects(input=None):
    image_data = _get_java_image_data(input)
    if image_data is not None:
        image_data.getHierarchy().clearAll()


def _delete_objects(image_data: JavaObject, path_objects: List[JavaObject]):
    if image_data is not None and path_objects:
        image_data.getHierarchy().removeObjects(path_objects, True)


def delete_detections(input=None):
    image_data = _get_java_image_data(input)
    if image_data is not None:
        _delete_objects(image_data=image_data, path_objects=image_data.getHierarchy().getDetectionObjects())


def delete_annotations(input=None):
    image_data = _get_java_image_data(input)
    if image_data is not None:
        _delete_objects(image_data=image_data, path_objects=image_data.getHierarchy().getAnnotationObjects())


def delete_cells(input=None):
    image_data = _get_java_image_data(input)
    if image_data is not None:
        _delete_objects(image_data=image_data, path_objects=image_data.getHierarchy().getCellObjects())


def delete_tiles(input=None):
    image_data = _get_java_image_data(input)
    if image_data is not None:
        _delete_objects(image_data=image_data, path_objects=image_data.getHierarchy().getTileObjects())


def get_server(input=None) -> ImageServer:
    """
    Get an ImageServer from the input
    """
    if isinstance(input, ImageServer):
        return input
    server = _get_java_server(input)
    return None if server is None else QuPathServer(
        gateway=_gateway_or_default(),
        server_obj=server)


def get_dask_array(input=None, downsamples: Union[float, Iterable[float]] = None, **kwargs) -> da.Array:
    """
    Get one or more dask arrays corresponding to the image currently open in QuPath.
    """
    server = get_server(input)
    from ..images import to_dask
    return None if server is None else to_dask(server, downsamples=downsamples, **kwargs)


def get_detections(input=None, **kwargs) -> List[Feature]:
    return get_objects(input, object_type=types.DETECTION, **kwargs)


def get_cells(input=None, **kwargs) -> List[Feature]:
    return get_objects(input, object_type=types.CELL, **kwargs)


def get_tiles(input=None, **kwargs) -> List[Feature]:
    return get_objects(input, object_type=types.TILE, **kwargs)


def get_tma_cores(input=None, **kwargs) -> List[Feature]:
    return get_objects(input, object_type=types.TMA_CORE, **kwargs)


def get_annotations(input=None, **kwargs) -> List[Feature]:
    return get_objects(input, object_type=types.ANNOTATION, **kwargs)


def get_objects(input=None, object_type: str = None, gateway=None, to_image_object=True) -> List[Feature]:
    gateway = _gateway_or_default(input, gateway)
    hierarchy = _get_java_hierarchy(input)
    if hierarchy is None:
        warnings.warn('No object hierarchy found')
        return []
    if object_type is None:
        path_objects = hierarchy.getAllObjects(False)
    elif object_type == types.ANNOTATION:
        path_objects = hierarchy.getAnnotationObjects()
    elif object_type == types.DETECTION:
        path_objects = hierarchy.getDetectionObjects()
    elif object_type == types.TILE:
        path_objects = hierarchy.getTileObjects()
    elif object_type == types.CELL:
        path_objects = hierarchy.getCellObjects()
    elif object_type == types.TMA_CORE:
        tma_grid = hierarchy.getTMAGrid()
        path_objects = [] if tma_grid is None else tma_grid.getTMACoreList()

    features = []
    # Use toFeatureCollections for performance and to avoid string length troubles
    for sublist in gateway.entry_point.toFeatureCollections(path_objects, 1000):
        features.extend(_geojson_features_from_string(sublist, parse_constant=None))

    if to_image_object:
        return [_as_image_object(f) for f in features]
    else:
        return features


def _geojson_features_from_string(json_string: str, **kwargs):
    """
    Read features from a GeoJSON string.
    If the string encodes a feature collection, the features themselves will be extracted.
    """
    results = _geojson_from_string(json_string, **kwargs)
    if 'features' in results:
        results = results['features']
    return results


def _geojson_from_string(json: str, **kwargs):
    """
    Read a GeoJSON string.
    This is a wrapper around geojson.loads that allows for NaNs by default (and is generally non-strict with numbers).
    """
    # Default parse constant is _enforce_strict_numbers, which fails on NaNs
    if 'parse_constant' in kwargs:
        return geojson.loads(json, **kwargs)
    else:
        return geojson.loads(json, parse_constant=None, **kwargs)


def _as_image_object(feature: Feature) -> ImageObject:
    geometry = _find_property(feature, 'geometry')

    plane = _find_property(feature, 'plane')
    if plane is not None:
        geometry = to_geometry(geometry, z=getattr(plane, 'z', None), t=getattr(plane, 't', None))

    args = dict(
        geometry=geometry,
        id=_find_property(feature, 'id'),
        classification=_find_property(feature, 'classification'),
        name=_find_property(feature, 'name'),
        color=_find_property(feature, 'color'),
        measurements=_find_property(feature, 'measurements'),
        object_type=_find_property(feature, 'object_type'),
    )

    nucleus_geometry = _find_property(feature, 'nucleusGeometry')
    if nucleus_geometry is not None:
        if plane is not None:
            nucleus_geometry = to_geometry(nucleus_geometry, z=getattr(plane, 'z', None), t=getattr(plane, 't', None))
        args['extra_geometries'] = dict(nucleus=nucleus_geometry)

    args['extra_properties'] = {k: v for k, v in feature['properties'].items() if k not in args and v is not None}
    return ImageObject(**args)


def _find_property(feature: Feature, property_name: str, default_value=None):
    if property_name in feature:
        return feature[property_name]
    if 'properties' in feature and property_name in feature['properties']:
        return feature['properties'][property_name]
    return default_value
