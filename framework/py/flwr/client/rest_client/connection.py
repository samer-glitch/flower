# Copyright 2025 Flower Labs GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Contextmanager for a REST request-response channel to the Flower server."""

from collections.abc import Iterator
from contextlib import contextmanager
from logging import DEBUG, ERROR, INFO, WARN
from typing import Callable, Optional, TypeVar, Union, cast

from cryptography.hazmat.primitives.asymmetric import ec
from google.protobuf.message import Message as GrpcMessage
from requests.exceptions import ConnectionError as RequestsConnectionError

from flwr.common import GRPC_MAX_MESSAGE_LENGTH
from flwr.common.constant import HEARTBEAT_DEFAULT_INTERVAL
from flwr.common.exit import ExitCode, flwr_exit
from flwr.common.heartbeat import HeartbeatSender
from flwr.common.inflatable import (
    get_all_nested_objects,
    get_object_tree,
    iterate_object_tree,
    no_object_id_recompute,
)
from flwr.common.inflatable_rest_utils import (
    make_pull_object_fn_rest,
    make_push_object_fn_rest,
)
from flwr.common.inflatable_utils import (
    inflate_object_from_contents,
    pull_objects,
    push_objects,
)
from flwr.common.logger import log
from flwr.common.message import Message, remove_content_from_message
from flwr.common.retry_invoker import RetryInvoker
from flwr.common.serde import message_to_proto, run_from_proto
from flwr.common.typing import Fab, Run
from flwr.proto.fab_pb2 import GetFabRequest, GetFabResponse  # pylint: disable=E0611
from flwr.proto.fleet_pb2 import (  # pylint: disable=E0611
    CreateNodeRequest,
    CreateNodeResponse,
    DeleteNodeRequest,
    DeleteNodeResponse,
    PullMessagesRequest,
    PullMessagesResponse,
    PushMessagesRequest,
    PushMessagesResponse,
)
from flwr.proto.heartbeat_pb2 import (  # pylint: disable=E0611
    SendNodeHeartbeatRequest,
    SendNodeHeartbeatResponse,
)
from flwr.proto.message_pb2 import (  # pylint: disable=E0611
    ConfirmMessageReceivedRequest,
    ConfirmMessageReceivedResponse,
    PullObjectRequest,
    PullObjectResponse,
    PushObjectRequest,
    PushObjectResponse,
)
from flwr.proto.node_pb2 import Node  # pylint: disable=E0611
from flwr.proto.run_pb2 import GetRunRequest, GetRunResponse  # pylint: disable=E0611

try:
    import requests
except ModuleNotFoundError:
    flwr_exit(ExitCode.COMMON_MISSING_EXTRA_REST)


PATH_CREATE_NODE: str = "api/v0/fleet/create-node"
PATH_DELETE_NODE: str = "api/v0/fleet/delete-node"
PATH_PULL_MESSAGES: str = "/api/v0/fleet/pull-messages"
PATH_PUSH_MESSAGES: str = "/api/v0/fleet/push-messages"
PATH_PULL_OBJECT: str = "/api/v0/fleet/pull-object"
PATH_PUSH_OBJECT: str = "/api/v0/fleet/push-object"
PATH_SEND_NODE_HEARTBEAT: str = "api/v0/fleet/send-node-heartbeat"
PATH_GET_RUN: str = "/api/v0/fleet/get-run"
PATH_GET_FAB: str = "/api/v0/fleet/get-fab"
PATH_CONFIRM_MESSAGE_RECEIVED: str = "/api/v0/fleet/confirm-message-received"

T = TypeVar("T", bound=GrpcMessage)


@contextmanager
def http_request_response(  # pylint: disable=R0913,R0914,R0915,R0917
    server_address: str,
    insecure: bool,  # pylint: disable=unused-argument
    retry_invoker: RetryInvoker,
    max_message_length: int = GRPC_MAX_MESSAGE_LENGTH,  # pylint: disable=W0613
    root_certificates: Optional[
        Union[bytes, str]
    ] = None,  # pylint: disable=unused-argument
    authentication_keys: Optional[  # pylint: disable=unused-argument
        tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]
    ] = None,
) -> Iterator[
    tuple[
        Callable[[], Optional[Message]],
        Callable[[Message], None],
        Callable[[], Optional[int]],
        Callable[[], None],
        Callable[[int], Run],
        Callable[[str, int], Fab],
    ]
]:
    """Primitives for request/response-based interaction with a server.

    One notable difference to the grpc_connection context manager is that
    `receive` can return `None`.

    Parameters
    ----------
    server_address : str
        The IPv6 address of the server with `http://` or `https://`.
        If the Flower server runs on the same machine
        on port 8080, then `server_address` would be `"http://[::]:8080"`.
    insecure : bool
        Unused argument present for compatibilty.
    retry_invoker: RetryInvoker
        `RetryInvoker` object that will try to reconnect the client to the server
        after REST connection errors. If None, the client will only try to
        reconnect once after a failure.
    max_message_length : int
        Ignored, only present to preserve API-compatibility.
    root_certificates : Optional[Union[bytes, str]] (default: None)
        Path of the root certificate. If provided, a secure
        connection using the certificates will be established to an SSL-enabled
        Flower server. Bytes won't work for the REST API.
    authentication_keys : Optional[Tuple[PrivateKey, PublicKey]] (default: None)
        Client authentication is not supported for this transport type.

    Returns
    -------
    receive : Callable
    send : Callable
    create_node : Optional[Callable]
    delete_node : Optional[Callable]
    get_run : Optional[Callable]
    """
    log(
        WARN,
        """
        EXPERIMENTAL: `rest` is an experimental feature, it might change
        considerably in future versions of Flower
        """,
    )

    base_url = server_address

    # NEVER SET VERIFY TO FALSE
    # Otherwise any server can fake its identity
    # Please refer to:
    # https://requests.readthedocs.io/en/latest/user/advanced/#ssl-cert-verification
    verify: Union[bool, str] = True
    if isinstance(root_certificates, str):
        verify = root_certificates
    elif isinstance(root_certificates, bytes):
        log(
            ERROR,
            "For the REST API, the root certificates "
            "must be provided as a string path to the client.",
        )
    if authentication_keys is not None:
        log(ERROR, "Client authentication is not supported for this transport type.")

    # Shared variables for inner functions
    node: Optional[Node] = None

    ###########################################################################
    # heartbeat/create_node/delete_node/receive/send/get_run functions
    ###########################################################################

    def _request(
        req: GrpcMessage, res_type: type[T], api_path: str, retry: bool = True
    ) -> Optional[T]:
        # Serialize the request
        req_bytes = req.SerializeToString()

        # Send the request
        def post() -> requests.Response:
            return requests.post(
                f"{base_url}/{api_path}",
                data=req_bytes,
                headers={
                    "Accept": "application/protobuf",
                    "Content-Type": "application/protobuf",
                },
                verify=verify,
                timeout=None,
            )

        if retry:
            res: requests.Response = retry_invoker.invoke(post)
        else:
            res = post()

        # Check status code and headers
        if res.status_code != 200:
            return None
        if "content-type" not in res.headers:
            log(
                WARN,
                "[Node] POST /%s: missing header `Content-Type`",
                api_path,
            )
            return None
        if res.headers["content-type"] != "application/protobuf":
            log(
                WARN,
                "[Node] POST /%s: header `Content-Type` has wrong value",
                api_path,
            )
            return None

        # Deserialize ProtoBuf from bytes
        grpc_res = res_type()
        grpc_res.ParseFromString(res.content)
        return grpc_res

    def send_node_heartbeat() -> bool:
        # Get Node
        if node is None:
            log(ERROR, "Node instance missing")
            return False

        # Construct the heartbeat request
        req = SendNodeHeartbeatRequest(
            node=node, heartbeat_interval=HEARTBEAT_DEFAULT_INTERVAL
        )

        # Send the request
        res = _request(
            req, SendNodeHeartbeatResponse, PATH_SEND_NODE_HEARTBEAT, retry=False
        )
        if res is None:
            return False

        # Check if success
        if not res.success:
            raise RuntimeError(
                "Heartbeat failed unexpectedly. The SuperLink does not "
                "recognize this SuperNode."
            )
        return True

    heartbeat_sender = HeartbeatSender(send_node_heartbeat)

    def create_node() -> Optional[int]:
        """Set create_node."""
        req = CreateNodeRequest(heartbeat_interval=HEARTBEAT_DEFAULT_INTERVAL)

        # Send the request
        res = _request(req, CreateNodeResponse, PATH_CREATE_NODE)
        if res is None:
            return None

        # Remember the node and start the heartbeat sender
        nonlocal node
        node = res.node
        heartbeat_sender.start()
        return node.node_id

    def delete_node() -> None:
        """Set delete_node."""
        nonlocal node
        if node is None:
            log(ERROR, "Node instance missing")
            return

        # Stop the heartbeat sender
        heartbeat_sender.stop()

        # Send DeleteNode request
        req = DeleteNodeRequest(node=node)

        # Send the request
        res = _request(req, DeleteNodeResponse, PATH_DELETE_NODE)
        if res is None:
            return

        # Cleanup
        node = None

    def receive() -> Optional[Message]:
        """Receive next Message from server."""
        # Get Node
        if node is None:
            log(ERROR, "Node instance missing")
            return None

        # Request instructions (message) from server
        req = PullMessagesRequest(node=node)

        # Send the request
        res = _request(req, PullMessagesResponse, PATH_PULL_MESSAGES)
        if res is None:
            return None

        # Get the current Messages
        message_proto = None if len(res.messages_list) == 0 else res.messages_list[0]

        # Discard the current message if not valid
        if message_proto is not None and not (
            message_proto.metadata.dst_node_id == node.node_id
        ):
            message_proto = None

        # Construct the Message
        in_message: Optional[Message] = None

        if message_proto:
            log(INFO, "[Node] POST /%s: success", PATH_PULL_MESSAGES)
            msg_id = message_proto.metadata.message_id
            run_id = message_proto.metadata.run_id

            def fn(request: PullObjectRequest) -> PullObjectResponse:
                res = _request(
                    req=request, res_type=PullObjectResponse, api_path=PATH_PULL_OBJECT
                )
                if res is None:
                    raise ValueError("PushObjectResponse is None.")
                return res

            try:
                object_tree = res.message_object_trees[0]
                all_object_contents = pull_objects(
                    [tree.object_id for tree in iterate_object_tree(object_tree)],
                    pull_object_fn=make_pull_object_fn_rest(
                        pull_object_rest=fn,
                        node=node,
                        run_id=run_id,
                    ),
                )

                # Confirm that the message has been received
                _request(
                    req=ConfirmMessageReceivedRequest(
                        node=node, run_id=run_id, message_object_id=msg_id
                    ),
                    res_type=ConfirmMessageReceivedResponse,
                    api_path=PATH_CONFIRM_MESSAGE_RECEIVED,
                )
            except ValueError as e:
                log(
                    ERROR,
                    "Pulling objects failed. Potential irrecoverable error: %s",
                    str(e),
                )
            in_message = cast(
                Message, inflate_object_from_contents(msg_id, all_object_contents)
            )
            # The deflated message doesn't contain the message_id (its own object_id)
            # Inject
            in_message.metadata.__dict__["_message_id"] = msg_id

        return in_message

    def send(message: Message) -> None:
        """Send Message result back to server."""
        # Get Node
        if node is None:
            log(ERROR, "Node instance missing")
            return

        with no_object_id_recompute():
            # Get all nested objects
            all_objects = get_all_nested_objects(message)
            object_tree = get_object_tree(message)

            # Serialize Message
            message_proto = message_to_proto(
                message=remove_content_from_message(message)
            )
            req = PushMessagesRequest(
                node=node,
                messages_list=[message_proto],
                message_object_trees=[object_tree],
            )

            # Send the request
            res = _request(req, PushMessagesResponse, PATH_PUSH_MESSAGES)
            if res:
                log(
                    INFO,
                    "[Node] POST /%s: success, created result %s",
                    PATH_PUSH_MESSAGES,
                    res.results,  # pylint: disable=no-member
                )

            if res and res.objects_to_push:
                objs_to_push = set(res.objects_to_push[message.object_id].object_ids)

                def fn(request: PushObjectRequest) -> PushObjectResponse:
                    res = _request(
                        req=request,
                        res_type=PushObjectResponse,
                        api_path=PATH_PUSH_OBJECT,
                    )
                    if res is None:
                        raise ValueError("PushObjectResponse is None.")
                    return res

                try:
                    push_objects(
                        all_objects,
                        push_object_fn=make_push_object_fn_rest(
                            push_object_rest=fn,
                            node=node,
                            run_id=message_proto.metadata.run_id,
                        ),
                        object_ids_to_push=objs_to_push,
                    )
                    log(DEBUG, "Pushed %s objects to servicer.", len(objs_to_push))
                except ValueError as e:
                    log(
                        ERROR,
                        "Pushing objects failed. Potential irrecoverable error: %s",
                        str(e),
                    )
                    log(ERROR, str(e))

    def get_run(run_id: int) -> Run:
        # Construct the request
        req = GetRunRequest(node=node, run_id=run_id)

        # Send the request
        res = _request(req, GetRunResponse, PATH_GET_RUN)
        if res is None:
            return Run.create_empty(run_id)

        return run_from_proto(res.run)

    def get_fab(fab_hash: str, run_id: int) -> Fab:
        # Construct the request
        req = GetFabRequest(node=node, hash_str=fab_hash, run_id=run_id)

        # Send the request
        res = _request(req, GetFabResponse, PATH_GET_FAB)
        if res is None:
            return Fab("", b"")

        return Fab(
            res.fab.hash_str,
            res.fab.content,
        )

    try:
        # Yield methods
        yield (receive, send, create_node, delete_node, get_run, get_fab)
    except Exception as exc:  # pylint: disable=broad-except
        log(ERROR, exc)
    # Cleanup
    finally:
        try:
            if node is not None:
                # Disable retrying
                retry_invoker.max_tries = 1
                delete_node()
        except RequestsConnectionError:
            pass
