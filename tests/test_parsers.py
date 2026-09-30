"""Spec parsers: every supported API type is detected and parsed into
normalised APISchema operations."""

from pathlib import Path

import pytest

from app.models.schemas import APIType
from app.parsers.asyncapi_parser import AsyncAPIParseError, AsyncAPIParser
from app.parsers.graphql_parser import GraphQLParser
from app.parsers.parser_factory import create_parser, detect_api_type
from app.parsers.proto_parser import ProtoParseError, ProtoParser
from app.parsers.wsdl_parser import WSDLParseError, WSDLParser
from app.swagger.swagger_parser import SwaggerParseError, SwaggerParser
from demo.mock_server import api as customers_api

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def _ops(schemas) -> set[tuple[str, str]]:
    return {(s.method, s.endpoint) for s in schemas}


# -- detection ---------------------------------------------------------------


@pytest.mark.positive
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/schema.graphql", APIType.GRAPHQL),
        ("https://example.com/schema.GQL?v=2", APIType.GRAPHQL),
        ("https://example.com/service.wsdl", APIType.SOAP),
        ("https://example.com/greeter.proto", APIType.GRPC),
        ("https://example.com/asyncapi.yaml", APIType.WEBSOCKET),
        ("https://example.com/openapi.json", APIType.REST),
    ],
)
def test_detect_api_type_from_url(url, expected):
    assert detect_api_type(spec_url=url) == expected


@pytest.mark.positive
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("schema.graphql", APIType.GRAPHQL),
        ("calculator.wsdl", APIType.SOAP),
        ("greeter.proto", APIType.GRPC),
        ("chat-asyncapi.yaml", APIType.WEBSOCKET),
    ],
)
def test_detect_api_type_from_content(fixture, expected):
    assert detect_api_type(spec_content=_read(fixture)) == expected


@pytest.mark.edge
def test_detect_api_type_defaults_to_rest():
    assert detect_api_type() == APIType.REST
    assert detect_api_type(spec_content="just some text") == APIType.REST


@pytest.mark.positive
@pytest.mark.parametrize(
    ("api_type", "parser_cls"),
    [
        (APIType.REST, SwaggerParser),
        (APIType.GRAPHQL, GraphQLParser),
        (APIType.SOAP, WSDLParser),
        (APIType.GRPC, ProtoParser),
        (APIType.WEBSOCKET, AsyncAPIParser),
    ],
)
def test_create_parser_returns_matching_parser(api_type, parser_cls):
    assert isinstance(create_parser(api_type), parser_cls)


# -- REST (OpenAPI 3, from the demo Customers API) ---------------------------


@pytest.mark.positive
def test_openapi_spec_is_parsed_into_every_operation():
    schemas = SwaggerParser().parse(customers_api.openapi())

    assert _ops(schemas) == {
        ("POST", "/customers"),
        ("GET", "/customers"),
        ("GET", "/customers/{customer_id}"),
        ("PUT", "/customers/{customer_id}"),
        ("DELETE", "/customers/{customer_id}"),
    }


@pytest.mark.positive
def test_openapi_required_fields_and_response_codes():
    schemas = SwaggerParser().parse(customers_api.openapi())
    create = next(s for s in schemas if (s.method, s.endpoint) == ("POST", "/customers"))

    assert set(create.required_fields) == {"name", "email"}
    assert {"201", "409", "422"} <= set(create.response_codes)


@pytest.mark.negative
def test_openapi_parser_rejects_a_spec_without_paths():
    with pytest.raises(SwaggerParseError):
        SwaggerParser().parse({"openapi": "3.0.0", "info": {}})


@pytest.mark.negative
def test_openapi_parser_rejects_a_missing_file():
    with pytest.raises(SwaggerParseError):
        SwaggerParser().parse("does-not-exist.yaml")


# -- GraphQL -------------------------------------------------------------------


@pytest.mark.positive
def test_graphql_sdl_operations():
    schemas = GraphQLParser().parse(_read("schema.graphql"))
    names = {(s.method.lower(), s.operation_name) for s in schemas}

    assert names == {
        ("query", "book"),
        ("query", "books"),
        ("mutation", "addBook"),
        ("subscription", "bookAdded"),
    }
    assert all(s.api_type == APIType.GRAPHQL for s in schemas)


# -- SOAP ------------------------------------------------------------------------


@pytest.mark.positive
def test_wsdl_operations():
    schemas = WSDLParser().parse(_read("calculator.wsdl"))

    assert [s.operation_name for s in schemas] == ["Add"]
    assert schemas[0].api_type == APIType.SOAP


@pytest.mark.negative
def test_wsdl_parser_rejects_invalid_xml():
    with pytest.raises(WSDLParseError):
        WSDLParser().parse("<definitions><unclosed>")


@pytest.mark.negative
def test_wsdl_parser_rejects_a_dict():
    with pytest.raises(WSDLParseError):
        WSDLParser().parse({"not": "xml"})


# -- gRPC ------------------------------------------------------------------------


@pytest.mark.positive
def test_proto_rpcs():
    schemas = ProtoParser().parse(_read("greeter.proto"))

    assert {s.operation_name for s in schemas} == {"SayHello", "StreamGreetings"}
    assert all(s.service_name == "Greeter" for s in schemas)
    assert all(s.api_type == APIType.GRPC for s in schemas)


@pytest.mark.edge
def test_proto_marks_server_streaming_rpc():
    schemas = {s.operation_name: s for s in ProtoParser().parse(_read("greeter.proto"))}

    assert schemas["SayHello"].method != schemas["StreamGreetings"].method


@pytest.mark.negative
def test_proto_parser_rejects_a_dict():
    with pytest.raises(ProtoParseError):
        ProtoParser().parse({"not": "proto"})


# -- WebSocket (AsyncAPI) --------------------------------------------------------


@pytest.mark.positive
def test_asyncapi_channel_send_and_receive():
    schemas = AsyncAPIParser().parse(_read("chat-asyncapi.yaml"))

    assert {s.endpoint for s in schemas} == {"chat/messages"}
    assert len(schemas) == 2
    assert all(s.api_type == APIType.WEBSOCKET for s in schemas)


@pytest.mark.negative
def test_asyncapi_parser_rejects_plain_text():
    with pytest.raises(AsyncAPIParseError):
        AsyncAPIParser().parse("not a spec")
