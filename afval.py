import datetime
import hashlib
import json
import pickle
import re
import typing as t
import urllib.parse
import zoneinfo
from enum import Enum

import fastapi as f
import httpx
import ics
import redis.asyncio as redis
from bs4 import BeautifulSoup
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

cache = redis.Redis(host="redis")


class NotFound(Exception):
    """Raised when a postcal_code + number doesn't result in data."""


class Provider(str, Enum):
    cleanprofs = "cleanprofs"
    afvalstoffen = "afvalstoffen"


class WasteType(str, Enum):
    non_recyclable = "non_recyclable"
    organic = "organic"
    paper = "paper"
    plastic = "plastic"
    tree = "tree"


NOT_FOUND = "__not_found__"
REDIS_POSITIVE_CACHE_EXPIRE = 3600
REDIS_NEGATIVE_CACHE_EXPIRE = 300

WASTE_TYPE_LABELS = {
    WasteType.non_recyclable: "Non Recyclable",
    WasteType.organic: "Organic",
    WasteType.paper: "Paper",
    WasteType.tree: "Tree",
}


CLEANPROFS_WASTE_TYPES = {
    "rst": WasteType.non_recyclable,
    "gft": WasteType.organic,
}


AFVALSTOFFEN_WASTE_TYPES = {
    "restafval": WasteType.non_recyclable,
    "gft": WasteType.organic,
    "papier": WasteType.paper,
    "kerstbomen": WasteType.tree,
}


MONTHS = [
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
]

MONTHS_ABBREVIATED = [
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
]

AMSTERDAM = zoneinfo.ZoneInfo("Europe/Amsterdam")


limiter = Limiter(key_func=get_remote_address)
app = f.FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def afvalstoffen_retrieve_calendar(
    postal_code: str, number: str, addition: str
) -> str:
    login_param = {
        "username": None,
        "password": None,
        "rememberMe": None,
        "postcode": postal_code,
        "huisnummer": number,
        "toevoeging": addition,
        "debtornumber": "",
    }
    cookies = {
        "loginParam": urllib.parse.quote(json.dumps(login_param)),
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://www.afvalstoffendienst.nl/bewoners/s-hertogenbosch"
        )

        if response.status_code != 200:
            raise NotFound()

        response = await client.get(
            "https://www.afvalstoffendienst.nl/afvalkalender", cookies=cookies
        )

        if response.status_code != 200:
            raise NotFound()

    return response.text


async def afvalstoffen_get_dates(
    postal_code: str, number: str, addition: str
) -> list[datetime.datetime, WasteType]:
    body = await afvalstoffen_retrieve_calendar(postal_code, number, addition)
    lines = body.split("\n")

    dates = []

    for line in lines:
        if match := re.match(r'^\s*<p class="([^"]+)">[^\s]+\s+(\d+) ([^<]+)', line):
            type_, day, month = match.groups()
            day = int(day)
            month = MONTHS.index(month.lower()) + 1

            pickup_date = datetime.date(datetime.date.today().year, month, day)
            container = AFVALSTOFFEN_WASTE_TYPES[type_]

            dates.append((pickup_date, container))

    return sorted(dates)


def cleanprofs_extract_item(element):
    lead_elements = element.find_all("span", class_="tb-lead")
    texts = [e.text.strip().lower() for e in lead_elements]

    try:
        waste_type = [
            CLEANPROFS_WASTE_TYPES[text]
            for text in texts
            if text in CLEANPROFS_WASTE_TYPES
        ][0]
    except IndexError:
        return None, None

    for text in texts:
        if match := re.match(r"^(\d+) ([a-z]{3})$", text):
            day, month = match.groups()
            date = datetime.date.today().replace(
                month=MONTHS_ABBREVIATED.index(month) + 1,
                day=int(day),
            )
            return date, waste_type

    return None, None


async def cleanprofs_download_items(postal_code: str, number: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://crm.cleanprofs.nl/search/planning",
            data=dict(
                zipcode=postal_code,
                street_number=number,
            ),
        )

    if response.status_code != 200:
        raise NotFound()

    soup = BeautifulSoup(response.text, "html.parser")

    items = []
    for element in soup.find_all("div", class_="nk-tb-item"):
        date, waste_type = cleanprofs_extract_item(element)

        if not waste_type:
            continue

        items.append((date, waste_type))

    return sorted(items)


async def call_cached(fun: t.Callable, *args):
    try:
        added, result = call_cached._cache[args]
    except KeyError:
        pass
    else:
        threshold = datetime.datetime.now() - datetime.timedelta(hours=5)
        if added > threshold:
            if result is NotFound:
                raise f.HTTPException(status_code=404)
            return result

    try:
        result = await fun(*args)
    except NotFound:
        call_cached._cache[args] = (datetime.datetime.now(), NotFound)
        raise f.HTTPException(status_code=404)
    else:
        call_cached._cache[args] = (datetime.datetime.now(), result)
        return result


call_cached._cache = {}


def create_calander(
    items: list[dict],
    *,
    item_prefix: str,
    begin: datetime.time,
    end: datetime.time,
    alarms: list[datetime.timedelta] | None = None,
) -> ics.Calendar:
    alarms = alarms or []

    calendar = ics.Calendar()

    for timestamp, waste_type in items:
        item_begin = datetime.datetime.combine(timestamp, begin).replace(
            tzinfo=AMSTERDAM
        )
        item_end = datetime.datetime.combine(timestamp, end).replace(tzinfo=AMSTERDAM)

        item_alarms = [
            ics.DisplayAlarm(trigger=item_begin + offset) for offset in alarms
        ]

        uid = hashlib.sha256()
        uid.update(item_prefix.encode("utf-8"))
        uid.update(str(timestamp).encode("utf-8"))
        uid.update(waste_type.value.encode("utf-8"))

        event = ics.Event(
            uid=uid.hexdigest(),
            name=f"{item_prefix}: {WASTE_TYPE_LABELS[waste_type.value]}",
            begin=item_begin,
            end=item_end,
            alarms=item_alarms,
        )

        calendar.events.add(event)

    return calendar


async def fetch_cached_data_for(
    key: str,
) -> list[tuple[datetime.date, WasteType]] | None:
    result = await cache.get(key)

    if result == NOT_FOUND:
        return NotFound
    elif result is None:
        return None

    return pickle.loads(result)


async def cache_negative_result(key: str) -> None:
    await cache.set(key, NOT_FOUND)
    await cache.expire(key, REDIS_NEGATIVE_CACHE_EXPIRE)


async def cache_positive_result(
    key: str, items: list[tuple[datetime.date, WasteType]]
) -> None:
    data = pickle.dumps(items)
    await cache.set(key, data)
    await cache.expire(key, REDIS_POSITIVE_CACHE_EXPIRE)


async def fetch_data_for(
    provider: Provider,
    postal_code: str,
    number: str,
    addition: str = "",
) -> list[tuple[datetime.date, WasteType]]:
    key = f"{provider.value}-{postal_code}-{number}-{addition}"
    items = await fetch_cached_data_for(key)

    if items is NotFound:
        raise f.HTTPException(status_code=404)
    elif items is not None:
        return items

    try:
        if provider is Provider.cleanprofs:
            items = await cleanprofs_download_items(postal_code, number)
        elif provider is Provider.afvalstoffen:
            items = await afvalstoffen_get_dates(postal_code, number, addition)
    except NotFound:
        await cache_negative_result(key)
        raise f.HTTPException(status_code=404)
    except:
        raise f.HTTPException(status_code=500)

    await cache_positive_result(key, items)
    return items


@app.get("/{provider}.json")
@limiter.limit("5/minute")
async def cleanprofs_json(
    request: f.Request,
    provider: Provider,
    postal_code: str,
    number: str,
    addition: str = "",
):
    items = await fetch_data_for(provider, postal_code, number, addition)
    return [
        {
            "date": date,
            "waste_type": waste_type,
        }
        for date, waste_type in items
    ]


@app.get("/{provider}.ics")
@limiter.limit("5/minute")
async def cleanprofs_ics(
    request: f.Request,
    provider: Provider,
    postal_code: str,
    number: str,
    addition: str = "",
    begin: datetime.time = datetime.time(7),
    end: datetime.time = datetime.time(19),
    alarms: list[datetime.timedelta] = f.Query(
        [datetime.timedelta(-12), datetime.timedelta(0)]
    ),
):
    items = await fetch_data_for(provider, postal_code, number, addition)
    prefix = "Cleanprofs" if provider is Provider.cleanprofs else "Afval"
    calendar = create_calander(
        items, item_prefix=prefix, begin=begin, end=end, alarms=alarms[:2]
    )
    return f.Response(calendar.serialize(), media_type="text/calendar")
