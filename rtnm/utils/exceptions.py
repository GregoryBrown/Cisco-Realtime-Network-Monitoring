"""
Global Real Time Network Monitoring exception and warning classes.
"""

class FormatDataError(Exception):
    """Formatting data failed with error"""
    pass

class DeviceFailedToConnect(Exception):
    """The device failed to connect"""
    pass

class DeviceDisconnected(Exception):
    """The device is disconnected"""
    pass

class DatabaseUploadFailed(Exception):
    """Unable to upload data to the database"""
    pass

class ElasticsearchUploadFailed(DatabaseUploadFailed):
    """Unable to upload data to elasticsearch database"""
    pass