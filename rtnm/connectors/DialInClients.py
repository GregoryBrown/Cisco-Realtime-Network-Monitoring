import grpc
import json
from logging import Logger, getLogger
from typing import List, Tuple, Generator, Union, Optional
from multiprocessing import Process, Queue, Value
from protos.cisco_mdt_dial_in_pb2_grpc import gRPCConfigOperStub
from protos.cisco_mdt_dial_in_pb2 import CreateSubsArgs
from protos.gnmi_pb2_grpc import gNMIStub
from protos.gnmi_pb2 import (
    Encoding,
    GetRequest,
    GetResponse,
    Subscription,
    SubscriptionList,
    SubscribeRequest,
    TypedValue
)
from utils.utils import create_gnmi_path


class DialInClient(Process):
    def __init__(self, connected: Value, data_queue: Queue, log_name: str, options: List[Tuple[str, str]] = None, timeout: int = 10000000, *args, **kwargs):
        super().__init__(name=kwargs["name"])
        if options is None:
            opts: List[Tuple[str, str]] = [("grpc.ssl_target_name_override", "ems.cisco.com")]
        self.options: List[Tuple[str, str]] = opts
        self._host: str = kwargs["address"]
        self._port: int = kwargs["port"]
        self.queue: Queue = data_queue
        self.log: Logger = getLogger(log_name)
        self._metadata: List[Tuple[str, str]] = [
            ("username", kwargs["username"]),
            ("password", kwargs["password"]),
        ]
        self._connected: Value = connected
        self._format: str = kwargs["format"]
        self.encoding: str = kwargs["encoding"]
        self.debug: bool = kwargs["debug"]
        self.retry: bool = kwargs["retry"]
        self.compression: bool = kwargs["compression"]
        if self._format == "gnmi":
            self.sub_mode = kwargs["subscription-mode"]
            self.sensors: List[str] = kwargs["sensors"]
            self.sample_interval: int = kwargs["sample-interval"]
            self.stream_mode = kwargs["stream-mode"]
        else:
            self.subs: List[str] = kwargs["subscriptions"]
        self._timeout: float = float(timeout)
        self.gnmi_stub = None
        self.cisco_ems_stub = None
        self.log.debug(f"Finished initialzing {self.name}")

    def _get_gnmi_stub(self) -> gNMIStub:
        # if not self.gnmi_stub:
        self.gnmi_stub: gNMIStub = gNMIStub(self.channel)
        return self.gnmi_stub

    def _get_ems_stub(self) -> gRPCConfigOperStub:
        # if not self.cisco_ems_stub:
        self.cisco_ems_stub: gRPCConfigOperStub = gRPCConfigOperStub(self.channel)
        return self.cisco_ems_stub

    def _get_version(self) -> str:
        stub: gNMIStub = self._get_gnmi_stub()
        get_message: GetRequest = GetRequest(
            path=[create_gnmi_path("openconfig-platform:components/component/state/software-version")],
            type=GetRequest.DataType.Value("STATE"),
            encoding=Encoding.Value("JSON_IETF"),
        )
        response: GetResponse = stub.Get(get_message, metadata=self._metadata)

        def _parse_version(version: GetResponse) -> str:
            for notification in version.notification:
                for update in notification.update:
                    version_rc_typed_value: TypedValue = update.val.json_ietf_val
                    version_rc_str: str = version_rc_typed_value.decode().strip("}").strip('"')
            return version_rc_str

        return _parse_version(response)

    def _get_hostname(self) -> str:
        stub: gNMIStub = self._get_gnmi_stub()
        get_message: GetRequest = GetRequest(
            path=[create_gnmi_path("Cisco-IOS-XR-shellutil-cfg:host-names")],
            type=GetRequest.DataType.Value("CONFIG"),
            encoding=Encoding.Value("JSON_IETF"),
        )
        response: GetResponse = stub.Get(get_message, metadata=self._metadata)

        def _parse_hostname(hostname_response: GetResponse) -> str:
            for notification in hostname_response.notification:
                for update in notification.update:
                    hostname: str = update.val.json_ietf_val
                    if not hostname:
                        return ""
                    return json.loads(hostname)["host-name"]

        return _parse_hostname(response)

    @staticmethod
    def sub_to_path(request):
        yield request

    def gnmi_subscribe(self) -> Generator[Optional[Tuple[str, str, str, str]], None, None]:
        subs: List[Subscription] = []
        version: str = self._get_version()
        hostname: str = self._get_hostname()
        for sensor in self.sensors:
            subs.append(
                Subscription(path=create_gnmi_path(sensor), mode=self.sub_mode,
                             sample_interval=self.sample_interval))
        sub_list: SubscriptionList = SubscriptionList(
            subscription=subs, mode=self.stream_mode, encoding=self.encoding,
        )
        sub_request: SubscribeRequest = SubscribeRequest(subscribe=sub_list)
        try:
            stub: gNMIStub = self._get_gnmi_stub()
            for response in stub.Subscribe(self.sub_to_path(sub_request), metadata=self._metadata):
                if response.error.message:
                    self.log.error(response.error.message)
                    self.log.error(response.error.code)
                    self._connected.value = False
                    yield None
                elif response.sync_response:
                    self.log.debug("Got all values atleast once")
                else:
                    yield ("gnmi", response.SerializeToString(), hostname, version)

        except Exception as error:
            self.log.error(error)
            self._connected.value = False
            yield None

    def ems_subscribe(self) -> Generator[Union[Tuple[str, str, None, None], None], None, None]:
        try:
            version: str = self._get_version()
            stub: gRPCConfigOperStub = self._get_ems_stub()
            sub_args: CreateSubsArgs = CreateSubsArgs(ReqId=1, encode=self.encoding,
                                                      Subscriptions=self.subs)
            for segment in stub.CreateSubs(sub_args, timeout=self._timeout,
                                           metadata=self._metadata):
                if segment.errors:
                    self.log.error(segment.errors)
                    self._connected.value = False
                    yield None
                else:
                    yield ("ems", segment.data, None, version)
        except Exception as error:
            self.log.error(error)
            yield None

    def connect(self):
        if self.compression:
            self.channel = grpc.insecure_channel(":".join([self._host, self._port]), self.options,
                                                 compression=grpc.Compression.Gzip)
        else:
            self.channel = grpc.insecure_channel(":".join([self._host, self._port]), self.options)
        try:
            grpc.channel_ready_future(self.channel).result(timeout=10)
            self._connected.value = True
            self.log.info("Connected")
        except grpc.FutureTimeoutError as error:
            self.log.error(f"Can't connect to {self._host}:{self._port}")
            self._connected.value = False

    def is_connected(self):
        return self._connected.value

    def disconnect(self):
        self.log.info(f"Closing channel for {self.name}")
        self.channel.close()

    def run(self):
        if self.retry:
            while self.retry:
                self.connect()
                if self.is_connected():
                    if self._format == "gnmi":
                        for response_bytes in self.gnmi_subscribe():
                            if response_bytes is None:
                                self.log.info("Received an error while streaming")
                                self.disconnect()
                            else:
                                self.queue.put_nowait(response_bytes)
                    else:
                        for response_bytes in self.ems_subscribe():
                            if response_bytes is None:
                                self.log.info("Received an error while streaming")
                                self.disconnect()
                            else:
                                self.queue.put_nowait(response_bytes)
        else:
            self.connect()
            if self.is_connected():
                if self._format == "gnmi":
                    for response_bytes in self.gnmi_subscribe():
                        if response_bytes is None:
                            self.log.info("Received an error while streaming")
                            self.disconnect()
                        else:
                            self.queue.put_nowait(response_bytes)
                else:
                    for response_bytes in self.ems_subscribe():
                        if response_bytes is None:
                            self.log.info("Received an error while streaming")
                            self.disconnect()
                        else:
                            self.queue.put_nowait(response_bytes)


class TLSDialInClient(DialInClient):
    def __init__(self, pem, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pem = pem

    def connect(self):
        credentials = grpc.ssl_channel_credentials(self._pem)
        if self.compression:
            self.channel = grpc.secure_channel(
                ":".join([self._host, self._port]), credentials, self.options, compression=grpc.Compression.Gzip)
        else:
            self.channel = grpc.secure_channel(":".join([self._host, self._port]), credentials, self.options)
        try:
            grpc.channel_ready_future(self.channel).result(timeout=10)
            self.log.info("Connected")
            self._connected.value = True
        except grpc.FutureTimeoutError as error:
            self.log.error(f"Can't connect to {self._host}:{self._port}")
            self._connected.value = False
