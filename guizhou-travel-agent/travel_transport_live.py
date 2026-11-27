"""Opt-in stdio transports. Launch only explicitly configured, existing providers."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import shutil

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from travel_catalog import public_catalog
from travel_state import BEIJING
from travel_transport import FlightAdapter, ProviderFailure, RailAdapter, WeatherAdapter

ROOT = Path(__file__).resolve().parent
MCP_ENV = {"PATH","SYSTEMROOT","WINDIR","TEMP","TMP","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY",
           "SSL_CERT_FILE","SSL_CERT_DIR","NODE_EXTRA_CA_CERTS","REQUESTS_CA_BUNDLE","CURL_CA_BUNDLE"}
TOOL_NAMES = {"open-meteo":{"weather_forecast"}, "12306":{"get-station-code-by-names","get-tickets"},
              "osm":{"route"},"flights":{"search_flights"},"fli":{"search_flights"}}


def transport_status(value):
    return {401:"auth_failed",403:"auth_failed",429:"rate_limited","bad_json":"invalid_response",
            "timeout":"timeout","connection_error":"provider_error"}.get(value,"provider_error")


def semantic_error(text):
    lower = text.casefold()
    if any(s in lower for s in ("429","rate limit","too many requests")):
        return ProviderFailure("rate_limited")
    if any(s in lower for s in ("401","403","unauthorized","forbidden","api key")):
        return ProviderFailure("auth_failed")
    if any(s in lower for s in ("timed out","timeout")):
        return ProviderFailure("timeout")
    if any(s in lower for s in ("no flights found","no flight results","未查询到车次","no route","noroute","nosegment")):
        return ProviderFailure("empty")
    return ProviderFailure("provider_error","upstream_rejected")


def unbox(reply):
    if reply.is_error:
        raise semantic_error(" ".join(getattr(item,"text","") for item in reply.content))
    if reply.structured_content is not None:
        data = reply.structured_content
        return data["result"] if set(data)=={"result"} else data
    try:
        return json.loads(next(item.text for item in reply.content if item.type=="text"))
    except (StopIteration, TypeError, json.JSONDecodeError) as exc:
        raise ProviderFailure("invalid_response","bad_json") from exc


def failure_in_group(error):
    if isinstance(error,ProviderFailure):
        return error
    if isinstance(error,BaseExceptionGroup):
        for member in error.exceptions:
            found=failure_in_group(member)
            if found is not None:
                return found
    return None


def stdio(provider, command, args, *, authorized, timeout=12, additional_read_only=()):
    if authorized is not True:
        raise PermissionError("缺少本轮只读联网授权")
    exe = shutil.which(str(command))
    if exe is None and Path(command).is_file():
        exe = str(Path(command).resolve())
    if exe is None:
        return None
    safe_env={k:v for k,v in os.environ.items() if k.upper() in MCP_ENV}
    if provider in {"open-meteo","12306","osm"}:
        safe_env.update(TRANSPORT="stdio",NODE_USE_ENV_PROXY="1")
    if provider=="osm":
        safe_env["OSM_USER_AGENT"]="GuizhouTravel/1.0"

    async def invoke(name, arguments):
        allowed=TOOL_NAMES[provider] | ({"find_nearby_pois"} & set(additional_read_only) if provider=="osm" else set())
        if name not in allowed:
            raise ProviderFailure("unbound")
        params = StdioServerParameters(command=exe,args=args,env=safe_env)
        try:
            async with asyncio.timeout(timeout):
                with open(os.devnull,"w") as err:
                    async with stdio_client(params,errlog=err) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            tool = next((tool for tool in tools.tools if tool.name==name),None)
                            if tool is None:
                                raise ProviderFailure("unbound")
                            declared=tool.input_schema.get("properties",{})
                            actual_args=dict(arguments)
                            if provider=="fli" and "passengers" not in declared:
                                actual_args.pop("passengers",None)
                            if set(actual_args)-declared.keys() or not Draft202012Validator(tool.input_schema).is_valid(actual_args):
                                raise ProviderFailure("invalid_response","provider_schema_mismatch")
                            return unbox(await session.call_tool(name,actual_args))
        except TimeoutError:
            raise
        except (ValueError,OSError,RuntimeError,ExceptionGroup) as exc:
            mapped=failure_in_group(exc)
            if mapped is not None:
                raise mapped from None
            raise ProviderFailure("provider_error","connection_error") from None

    return invoke


def node_tool(provider, raw_entry, *, authorized, timeout=12):
    if not raw_entry:
        return None
    entry=Path(raw_entry)
    filename={"open-meteo":("dist","index.js"),"12306":("build","index.js"),"osm":("dist","index.js")}[provider]
    if not entry.is_file() or entry.parts[-2:] != filename:
        return None
    return stdio(provider,"node",[str(entry.resolve())],authorized=authorized,timeout=timeout)


def primary_flights(*, authorized, timeout=12):
    spec=json.loads((ROOT/".mcp.json").read_text(encoding="utf-8-sig"))["mcpServers"]["flights"]
    if spec.get("args") not in (["-B","-m","flights_mcp"],["-m","flights_mcp"]):
        return None
    return stdio("flights",spec["command"],spec["args"],authorized=authorized,timeout=timeout)


def backup_flights(command, *, authorized, timeout=12):
    if not command or Path(command).name.lower() not in {"fli-mcp","fli-mcp.exe"}:
        return None
    call=stdio("fli",command,[],authorized=authorized,timeout=timeout)
    if call is not None:
        call.request_passengers=True
    return call


def configured_skills(clock=None):
    """Network is disabled by default. Each provider also requires its own switch/path."""
    if os.getenv("TRAVEL_M07_NETWORK")!="1":
        return None
    from travel_transport_skills import TransportSkills

    clock=clock or (lambda:datetime.now(BEIJING))
    catalog=public_catalog()
    weather=node_tool("open-meteo",os.getenv("TRAVEL_M07_WEATHER_ENTRY"),authorized=True)
    rail=node_tool("12306",os.getenv("TRAVEL_M07_RAIL_ENTRY"),authorized=True)
    route=node_tool("osm",os.getenv("TRAVEL_M07_OSM_ENTRY"),authorized=True)
    main=primary_flights(authorized=True) if os.getenv("TRAVEL_M07_FLIGHTS")=="1" else None
    backup=backup_flights(os.getenv("TRAVEL_M07_FLI"),authorized=True)
    return TransportSkills(catalog,clock,mode="live",route=route,
                           weather=WeatherAdapter(catalog,weather,clock,mode="live") if weather else None,
                           rail=RailAdapter(catalog,rail,clock,mode="live") if rail else None,
                           flight=FlightAdapter(catalog,main,backup,clock,mode="live",allow_backup=backup is not None)
                           if main or backup else None)

