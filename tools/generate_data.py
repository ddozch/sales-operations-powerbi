#!/usr/bin/env python3
"""Generate the synthetic Dynamics 365 / Dataverse export for the SalesOps model and the figures it must show.

Writes:
  data/export_info.csv        export timestamp (defines the Report Date)
  data/systemuser.csv         users: sales managers, installation coordinators, back office
  data/account.csv            customers
  data/product.csv            product catalogue
  data/salesorder.csv         order headers
  data/salesorderdetail.csv   order lines, including write-in lines without a product
  data/ops_installation.csv   installation jobs (custom table, like a work order)
  data/expected.json          the figures the Power BI model must return

Every record is generated as truth first: Python objects with time-zone aware UTC timestamps. The CSVs
are written from that truth like a Dataverse export (logical column names, UTC timestamps, date-only
columns as they are). The expected figures are calculated from the same truth with the metric rules of
the spec, implemented here independently of the M and DAX code. Local dates come from
zoneinfo("Europe/Warsaw"), not from the fixed CET/CEST rule the M code implements, so a mistake in
either one shows up as a difference.

Self-checks stop the script when the data breaks its own rules (order totals, job consistency,
edge-case minimums, data-quality shape) or when the written CSVs do not read back to the truth.
Fixed seed: the output is the same on every run.

Usage:  python -I tools/generate_data.py [--seed 2610]
"""
from __future__ import annotations

import argparse
import bisect
import copy
import csv
import json
import math
import random
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import accumulate
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

UTC = timezone.utc
WARSAW = ZoneInfo("Europe/Warsaw")
EXPORTED_ON = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
REPORT_DATE = EXPORTED_ON.astimezone(WARSAW).date()        # 2026-10-28
FIRST_ORDER_DAY, LAST_ORDER_DAY = date(2025, 1, 1), date(2026, 10, 27)
DATE_TABLE = (date(2025, 1, 1), date(2026, 12, 31))       # the model's Date table
LAST_PLANNED_MONTH = date(2026, 12, 1)                     # planned months stay inside the Date table
DST_DAYS = (date(2025, 3, 30), date(2025, 10, 26), date(2026, 3, 29), date(2026, 10, 25))
TARGET = 0.95
CENT = Decimal("0.01")
SOURCE = "Dynamics 365 / Dataverse export (synthetic demo data)"
NO_OWNER, NOT_SET, NOT_ASSIGNED, WRITE_IN_GROUP = "(no owner)", "(not set)", "(not assigned)", "Other"

# ---------------------------------------------------------------- people, places, products

SALES = ["Leo Brooks", "Mia Lane", "Nina Hart", "Owen Price", "Sam Carter"]
FORMER = "Ethan Cole"                 # former sales manager: disabled, still owns 2025 orders and accounts
FORMER_LAST_DAY = date(2025, 8, 29)
SUCCESSOR = "Mia Lane"                # took over most of his customers, but not the account records
# One name with Polish letters: the coordinator column is compared row by row, so it also tests the UTF-8 decoding
COORDINATORS = ["Adam Ward", "Grace Holt", "Paweł Król", "Julia Reed"]
BACK_OFFICE = ["Kate Bell", "Tom Fisher"]
CREWS = ["Central", "East", "North", "South", "West"]

CITY_WEIGHTS = {"Warszawa": 24, "Kraków": 15, "Łódź": 14, "Wrocław": 13, "Poznań": 11, "Gdańsk": 10,
                "Szczecin": 7, "Lublin": 6}
CITY_CREW = {"Warszawa": "East", "Lublin": "East", "Łódź": "Central", "Kraków": "South", "Wrocław": "West",
             "Poznań": "West", "Gdańsk": "North", "Szczecin": "North"}

NAME_WORDS = ["Harborview", "Oakridge", "Northgate", "Riverside", "Summit", "Clearview", "Westbrook", "Lakeside",
              "Pinecrest", "Redwood", "Fairway", "Stonehill", "Brightway", "Ironbridge", "Maplewood", "Silverline",
              "Greenfield", "Bluewater", "Cedar Grove", "Eastwood", "Highland", "Kingsbridge", "Meadowbrook",
              "Millstone", "Norwood", "Parkside", "Queensgate", "Rosewood", "Sunfield", "Thornbury", "Willowbank",
              "Ashford", "Birchwood", "Copperfield", "Goldcrest", "Hillcrest", "Juniper", "Linden", "Moorland",
              "Oakmont"]
SEGMENT_TYPES = {   # the name tells the segment, as in a real customer list
    "Property manager": ["Properties", "Property Management", "Estates", "Real Estate"],
    "Housing cooperative": ["Housing Cooperative", "Residents Cooperative", "Housing Association"],
    "Office building": ["Offices", "Business Park", "Office Tower", "Plaza"],
    "Hotel": ["Hotel", "Hotels", "Suites"],
    "Public sector": ["School", "City Library", "Health Centre", "Sports Hall"],
}
SEGMENT_SHARE = {"Property manager": 34, "Housing cooperative": 24, "Office building": 22, "Hotel": 10,
                 "Public sector": 10}
# Names with a comma or a double quote: the CSV must quote them (RFC 4180) and Power Query must read them back
SPECIAL_ACCOUNTS = [("Harborview Offices, Block B", "Office building"),
                    ('"Summit" Housing Cooperative', "Housing cooperative"),
                    ('Oakridge Residences "North", Phase 2', "Property manager"),
                    ("Lakeside Plaza, Sp. z o.o.", "Office building")]
ACCOUNTS, IMPORTED_ACCOUNTS = 420, 170    # imported = loaded when the CRM went live on 2025-01-01

# number, name, group, installable, list price EUR, quantity range per line
PRODUCTS = [
    ("AL-101", "Alarm control panel", "Alarm systems", True, "420.00", (1, 3)),
    ("AL-102", "Motion detector", "Alarm systems", True, "38.00", (4, 60)),
    ("AL-103", "Door contact", "Alarm systems", True, "14.50", (6, 80)),
    ("AL-104", "Indoor siren", "Alarm systems", True, "46.00", (1, 8)),
    ("AL-105", "Smoke detector", "Alarm systems", True, "29.90", (4, 60)),
    ("AL-190", "Alarm spare battery", "Alarm systems", False, "12.00", (2, 20)),     # delivered, not installed
    ("CC-201", "Dome camera 4MP", "CCTV", True, "145.00", (2, 32)),
    ("CC-202", "Bullet camera 8MP", "CCTV", True, "219.00", (2, 24)),
    ("CC-203", "Network video recorder 16ch", "CCTV", True, "690.00", (1, 2)),
    ("CC-204", "PoE switch 16 port", "CCTV", True, "310.00", (1, 3)),
    ("CC-205", "Camera mounting kit", "CCTV", True, "22.00", (2, 32)),
    ("AC-301", "Card reader", "Access control", True, "165.00", (2, 24)),
    ("AC-302", "Electric door strike", "Access control", True, "89.00", (2, 24)),
    ("AC-303", "Access controller 4 doors", "Access control", True, "540.00", (1, 6)),
    ("AC-304", "Video intercom panel", "Access control", True, "480.00", (1, 4)),
    ("WF-401", "Wi-Fi 6 access point", "Wi-Fi", True, "189.00", (2, 40)),
    ("WF-402", "Wi-Fi 6E access point", "Wi-Fi", True, "265.00", (2, 30)),
    ("WF-403", "Network controller", "Wi-Fi", True, "720.00", (1, 1)),
    ("WF-404", "Outdoor access point", "Wi-Fi", True, "340.00", (1, 8)),
    ("WF-490", "Cloud management licence (1 year)", "Wi-Fi", False, "35.00", (2, 40)),  # licence, not installed
    ("SV-501", "Site survey", "Services", False, "450.00", (1, 1)),
    ("SV-502", "Commissioning and handover", "Services", False, "380.00", (1, 2)),
    ("SV-503", "Annual maintenance", "Services", False, "950.00", (1, 1)),
    ("SV-504", "Staff training", "Services", False, "280.00", (1, 3)),
]
FOCUS_GROUPS = {"Alarm systems": 30, "CCTV": 27, "Access control": 18, "Wi-Fi": 25}
WRITE_IN_TEXTS = ["Extra cabling", "Custom mounting work", "Travel costs", "Scaffolding rental", "Cable trays",
                  "Additional programming", "Night work surcharge"]

BASE_ORDERS = {2025: 106, 2026: 121}      # orders per month before seasonality
SEASON = {1: 0.80, 2: 0.90, 3: 1.05, 4: 1.10, 5: 1.10, 6: 1.05, 7: 0.90, 8: 0.80, 9: 1.10, 10: 1.15, 11: 1.10,
          12: 0.85}
HOLIDAYS = {(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 24), (12, 25), (12, 26)}
PLANNED_MONTH_FILL = {"Owen Price": 0.62, FORMER: 0.90, None: 0.85}   # everyone else 0.96

# Timestamps near local midnight: the UTC date differs from the local date (comments give local time).
# Summer-time records at 22:xx UTC also need the +2 offset: a fixed +1 would keep them on the UTC date.
# Winter-time records at 22:xx UTC just outside the summer period must NOT move: a rule that applies
# summer time too long (fixed +2, US dates, a switch a week off) would push them to the next day.
ORDER_EDGE_TIMES = [
    "2024-12-31T23:45:00Z",  # 2025-01-01 00:45 CET   first order of 2025; its UTC date is in 2024
    "2025-03-29T23:40:00Z",  # 2025-03-30 00:40 CET   DST switch day
    "2025-03-30T22:20:00Z",  # 2025-03-31 00:20 CEST  first night on summer time
    "2025-03-31T22:30:00Z",  # 2025-04-01 00:30 CEST  month boundary
    "2025-07-14T22:30:00Z",  # 2025-07-15 00:30 CEST
    "2025-10-25T22:30:00Z",  # 2025-10-26 00:30 CEST  DST switch day, still summer time
    "2025-10-26T23:10:00Z",  # 2025-10-27 00:10 CET   first night on winter time
    "2025-10-31T23:20:00Z",  # 2025-11-01 00:20 CET   month boundary
    "2025-12-31T23:05:00Z",  # 2026-01-01 00:05 CET   year boundary
    "2025-12-31T23:30:00Z",  # 2026-01-01 00:30 CET   year boundary
    "2026-03-28T23:45:00Z",  # 2026-03-29 00:45 CET   DST switch day
    "2026-03-31T22:30:00Z",  # 2026-04-01 00:30 CEST  month boundary
    "2026-06-30T22:10:00Z",  # 2026-07-01 00:10 CEST  month boundary
    "2026-08-31T22:50:00Z",  # 2026-09-01 00:50 CEST  month boundary
    "2026-10-24T22:40:00Z",  # 2026-10-25 00:40 CEST  DST switch day
    "2026-10-25T23:15:00Z",  # 2026-10-26 00:15 CET   first night on winter time
    "2026-02-27T22:50:00Z",  # 2026-02-27 23:50 CET   late evening: the conversion must NOT move it
    "2025-05-30T21:40:00Z",  # 2025-05-30 23:40 CEST  late evening: the conversion must NOT move it
    "2025-03-29T22:30:00Z",  # 2025-03-29 23:30 CET   day before the spring switch: must NOT move
    "2025-10-26T22:30:00Z",  # 2025-10-26 23:30 CET   autumn switch day, after the switch: must NOT move
    "2025-10-31T22:30:00Z",  # 2025-10-31 23:30 CET   must stay in October (US dates or a late switch move it)
    "2026-03-28T22:40:00Z",  # 2026-03-28 23:40 CET   day before the spring switch: must NOT move
    "2026-10-25T22:20:00Z",  # 2026-10-25 23:20 CET   autumn switch day, after the switch: must NOT move
]
ACCOUNT_EDGE_TIMES = [
    "2024-12-31T23:30:00Z",  # 2025-01-01 00:30 CET   year boundary (customer of the first 2025 order)
    "2025-03-30T22:10:00Z",  # 2025-03-31 00:10 CEST
    "2025-10-25T22:40:00Z",  # 2025-10-26 00:40 CEST  DST switch day
    "2025-12-31T23:50:00Z",  # 2026-01-01 00:50 CET   year boundary
    "2026-01-31T23:15:00Z",  # 2026-02-01 00:15 CET   month boundary
    "2026-03-29T22:45:00Z",  # 2026-03-30 00:45 CEST
    "2026-10-25T23:05:00Z",  # 2026-10-26 00:05 CET
    "2025-03-29T22:20:00Z",  # 2025-03-29 23:20 CET   must NOT move
    "2025-10-31T22:35:00Z",  # 2025-10-31 23:35 CET   must stay in October (DQ by month)
    "2026-10-25T22:35:00Z",  # 2026-10-25 23:35 CET   must NOT move
]
INSTALL_EDGES = [            # (which timestamp is moved, UTC value)
    ("end", "2025-03-30T22:15:00Z"),      # 2025-03-31 00:15 CEST
    ("end", "2025-06-30T22:30:00Z"),      # 2025-07-01 00:30 CEST  month boundary for Units Installed
    ("end", "2025-10-25T22:45:00Z"),      # 2025-10-26 00:45 CEST  DST switch day
    ("end", "2025-12-31T23:40:00Z"),      # 2026-01-01 00:40 CET   year boundary for Units Installed
    ("created", "2025-12-31T23:20:00Z"),  # 2026-01-01 00:20 CET   numbered INS-26-..., DQ month 2026-01
    ("end", "2026-03-29T22:30:00Z"),      # 2026-03-30 00:30 CEST
    ("end", "2026-03-31T22:20:00Z"),      # 2026-04-01 00:20 CEST  month boundary
    ("end", "2026-10-24T22:50:00Z"),      # 2026-10-25 00:50 CEST  DST switch day
    ("end", "2026-10-27T23:30:00Z"),      # 2026-10-28 00:30 CET   ends on the Report Date: still counts
    ("start", "2026-10-28T05:30:00Z"),    # 2026-10-28 06:30 CET   started on the Report Date, before the export
    ("end", "2025-03-29T22:40:00Z"),      # 2025-03-29 23:40 CET   must NOT move
    ("end", "2025-10-26T22:20:00Z"),      # 2025-10-26 23:20 CET   must NOT move
    ("end", "2025-10-31T22:45:00Z"),      # 2025-10-31 23:45 CET   must stay in October for Units Installed
    ("end", "2026-03-28T22:30:00Z"),      # 2026-03-28 23:30 CET   must NOT move
    ("end", "2026-10-25T22:40:00Z"),      # 2026-10-25 23:40 CET   must NOT move
]

STATECODES = {0: "Active", 1: "Submitted", 2: "Canceled", 3: "Fulfilled"}
JOB_STATUS = {"Not scheduled": 1, "Scheduled": 100000001, "In progress": 100000002, "Completed": 2,
              "Cancelled": 100000003}
INSTALLATION_STATUSES = ["Installed", "In progress", "Delayed start", "Scheduled", "To schedule",
                         "No installation", "Cancelled"]
DQ_FIELDS = [   # key, field, record type, responsible department
    (1, "City", "Customer", "Sales"),
    (2, "Segment", "Customer", "Sales"),
    (3, "Owner", "Order", "Sales"),
    (4, "Planned install month", "Order", "Sales"),
    (5, "Product", "Order line", "Sales"),
    (6, "Crew", "Installation", "Installation"),
    (7, "Coordinator", "Installation", "Installation"),
    (8, "Actual start", "Installation", "Installation"),
    (9, "Specification date", "Installation", "Back office"),
]
DEPARTMENTS = ["Sales", "Installation", "Back office"]


# ---------------------------------------------------------------- truth records

@dataclass
class User:
    id: str
    name: str
    department: str
    disabled: bool


@dataclass
class Account:
    id: str
    name: str
    city: str | None
    segment: str | None
    owner: User
    created: datetime
    weight: float


@dataclass
class Product:
    id: str
    number: str
    name: str
    group: str
    installable: bool
    price: Decimal
    qty: tuple[int, int]


@dataclass
class Line:
    id: str
    product: Product | None          # None = write-in line
    description: str | None
    qty: int
    price: Decimal
    discount: Decimal
    amount: Decimal


@dataclass
class Job:
    id: str
    order: "Order"
    units: int                       # units this job is meant to install
    created: datetime
    status: str = "Not scheduled"
    crew: str | None = None
    coordinator: User | None = None
    planned_start: date | None = None
    planned_end: date | None = None
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    installed_units: int = 0
    spec_date: date | None = None
    name: str = ""
    tag: str = ""                    # edge case it was made for


@dataclass
class Order:
    id: str
    account: Account
    owner: User | None
    created: datetime
    signed: datetime | None = None
    planned_month: date | None = None
    cancelled: bool = False
    statecode: int = 0
    lines: list[Line] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    number: str = ""
    tag: str = ""

    @property
    def total(self) -> Decimal:
        return sum((l.amount for l in self.lines), Decimal(0))


# ---------------------------------------------------------------- date helpers

def local_date(ts: datetime | None) -> date | None:
    return ts.astimezone(WARSAW).date() if ts is not None else None


def at_local(d: date, minutes: int) -> datetime:
    """Warsaw wall-clock time on day d (minutes after midnight), returned in UTC."""
    return datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=WARSAW).astimezone(UTC)


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def utc_text(ts: datetime | None) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else ""


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def month_end(d: date) -> date:
    return next_month(d) - timedelta(days=1)


def shift_year(d: date) -> date:
    return d.replace(year=d.year - 1)    # no 29 February in the shifted range (2025-2026 -> 2024-2025)


def weekday_on_or_after(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def days(n: int) -> timedelta:
    return timedelta(days=n)


def fail(msg: str) -> None:
    raise SystemExit(f"SELF-CHECK FAILED: {msg}")


def check(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


# ---------------------------------------------------------------- generation

class Generator:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.users: list[User] = []
        self.user = {}
        self.products: list[Product] = []
        self.accounts: list[Account] = []
        self.orders: list[Order] = []
        self.jobs: list[Job] = []
        self.make_users()
        self.make_products()
        self.make_accounts()
        self.make_orders()
        self.edge_installation_times()
        self.edge_in_progress()
        self.edge_planned_starts()
        self.edge_cancelled_jobs()
        self.edge_cancelled_orders_with_live_jobs()
        self.edge_signed_times()
        self.edge_multiline_texts()
        self.finish()

    def guid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def make_users(self) -> None:
        specs = ([(n, "Sales", False) for n in SALES] + [(FORMER, "Sales", True)]
                 + [(n, "Installation", False) for n in COORDINATORS] + [(n, "Back office", False) for n in BACK_OFFICE])
        for name, dept, disabled in specs:
            u = User(self.guid(), name, dept, disabled)
            self.users.append(u)
            self.user[name] = u

    def make_products(self) -> None:
        for number, name, group, inst, price, qty in PRODUCTS:
            self.products.append(Product(self.guid(), number, name, group, inst, Decimal(price), qty))

    def account_owner(self, d: date) -> User:
        if d <= FORMER_LAST_DAY:
            names, weights = SALES + [FORMER], [21, 20, 18, 17, 14, 10]
        else:
            names, weights = SALES, [23, 21, 20, 19, 17]
        return self.user[self.rng.choices(names, weights)[0]]

    def make_accounts(self) -> None:
        rng = self.rng
        taken = {n for n, _ in SPECIAL_ACCOUNTS}
        picks = []
        while len(picks) < ACCOUNTS - len(SPECIAL_ACCOUNTS):
            seg = rng.choices(list(SEGMENT_SHARE), list(SEGMENT_SHARE.values()))[0]
            name = f"{rng.choice(NAME_WORDS)} {rng.choice(SEGMENT_TYPES[seg])}"
            if name not in taken:
                taken.add(name)
                picks.append((name, seg))
        picks += SPECIAL_ACCOUNTS
        rng.shuffle(picks)

        times = [(at_local(date(2025, 1, 1), rng.randint(20 * 60, 21 * 60 + 59)), True)
                 for _ in range(IMPORTED_ACCOUNTS)]
        times += [(parse_utc(t), False) for t in ACCOUNT_EDGE_TIMES]
        span = (date(2026, 10, 26) - date(2025, 1, 2)).days
        while len(times) < len(picks):
            d = date(2025, 1, 2) + days(rng.randint(0, span))
            if d.weekday() >= 5 and rng.random() < 0.85:
                continue
            times.append((at_local(d, rng.randint(8 * 60, 17 * 60)), False))

        cities, city_w = list(CITY_WEIGHTS), list(CITY_WEIGHTS.values())
        for (name, seg), (created, imported) in zip(picks, times):
            weight = rng.lognormvariate(0, 0.9) * (1.4 if imported else 1.0) * (0.6 if seg == "Public sector" else 1.0)
            self.accounts.append(Account(
                id=self.guid(), name=name,
                city=None if rng.random() < 0.09 else rng.choices(cities, city_w)[0],
                segment=None if rng.random() < 0.14 else seg,
                owner=self.account_owner(local_date(created)), created=created, weight=weight))
        self.accounts.sort(key=lambda a: (a.created, a.name))

    def order_times(self) -> list[datetime]:
        rng = self.rng
        times = []
        m = date(2025, 1, 1)
        while m <= LAST_ORDER_DAY:
            all_days = [m + days(n) for n in range((month_end(m) - m).days + 1)]
            in_range = [d for d in all_days if d <= LAST_ORDER_DAY]
            n = BASE_ORDERS[m.year] * SEASON[m.month] * rng.uniform(0.92, 1.08) * len(in_range) / len(all_days)
            weights = [0.03 if (d.month, d.day) in HOLIDAYS else 0.12 if d.weekday() == 5 else
                       0.05 if d.weekday() == 6 else 1.0 for d in in_range]
            for d in rng.choices(in_range, weights, k=round(n)):
                minute = rng.randint(450, 1110) if rng.random() < 0.93 else rng.randint(1110, 1290)
                times.append(at_local(d, minute))
            m = next_month(m)
        times += [parse_utc(t) for t in ORDER_EDGE_TIMES]
        return sorted(times)

    def make_orders(self) -> None:
        acc_times = [a.created for a in self.accounts]
        cum = list(accumulate(a.weight for a in self.accounts))
        without_orders = {a.id for a in self.accounts}
        for t in self.order_times():
            k = bisect.bisect_right(acc_times, t - timedelta(minutes=10))   # customers that exist already
            check(k > 0, f"no customer exists before the order at {utc_text(t)}")
            # a new customer is usually created for its first order, so it orders soon after
            fresh = [a for a in self.accounts[:k] if a.id in without_orders and t - a.created <= days(45)]
            if fresh and self.rng.random() < 0.35:
                account = self.rng.choice(fresh)
            else:
                account = self.rng.choices(self.accounts[:k], cum_weights=cum[:k])[0]
            without_orders.discard(account.id)
            self.orders.append(self.make_order(t, account))

    def order_owner(self, account: Account, d: date) -> User | None:
        rng = self.rng
        if account.owner.name == FORMER and d > FORMER_LAST_DAY:
            owner = self.user[SUCCESSOR if rng.random() < 0.7 else rng.choice(SALES)]
        elif rng.random() < 0.9:
            owner = account.owner
        else:
            owner = self.user[rng.choice(SALES + ([FORMER] if d <= FORMER_LAST_DAY else []))]
        return None if rng.random() < 0.028 else owner

    def make_line(self, product: Product | None) -> Line:
        rng = self.rng
        if product is None:
            qty = rng.randint(1, 12)
            price = Decimal(rng.randint(15, 250)).quantize(CENT)
            desc = rng.choice(WRITE_IN_TEXTS)
        else:
            qty = rng.randint(*product.qty)
            price = product.price
            if rng.random() < 0.15:     # negotiated unit price
                price = (price * Decimal(str(round(rng.uniform(0.90, 1.05), 3)))).quantize(CENT)
            desc = None
        gross = price * qty
        discount = Decimal(0).quantize(CENT)
        if rng.random() < 0.22:
            discount = (gross * Decimal(rng.choice([2, 3, 5, 7, 10, 12])) / 100).quantize(CENT)
        return Line(self.guid(), product, desc, qty, price, discount, (gross - discount).quantize(CENT))

    def make_lines(self) -> list[Line]:
        rng = self.rng
        by_number = {p.number: p for p in self.products}
        services = [p for p in self.products if p.group == "Services"]
        r = rng.random()
        if r < 0.055:      # maintenance or training only: nothing to install
            picks = rng.sample(services, rng.choice([1, 1, 2]))
        elif r < 0.065:    # licences or spare parts only: delivered, not installed
            picks = rng.sample([by_number["WF-490"], by_number["AL-190"]], rng.choice([1, 2]))
        else:
            focus = rng.choices(list(FOCUS_GROUPS), list(FOCUS_GROUPS.values()))[0]
            hardware = [p for p in self.products if p.group == focus and p.installable]
            picks = rng.sample(hardware, min(len(hardware), rng.choice([1, 1, 2, 2, 3, 4])))
            if rng.random() < 0.25:
                other = [p for p in self.products if p.installable and p.group != focus]
                picks.append(rng.choice(other))
            if focus == "Wi-Fi" and rng.random() < 0.4:
                picks.append(by_number["WF-490"])
            if focus == "Alarm systems" and rng.random() < 0.15:
                picks.append(by_number["AL-190"])
            for number, p in (("SV-501", 0.30), ("SV-502", 0.35), ("SV-504", 0.10), ("SV-503", 0.08)):
                if rng.random() < p:
                    picks.append(by_number[number])
            if rng.random() < 0.12:
                picks.append(None)
            picks = picks[:6]
        return [self.make_line(p) for p in picks]

    def clamp(self, base: datetime, ts: datetime) -> datetime:
        """Keep a generated timestamp before the export: nothing in the export can be newer than the export."""
        if ts <= EXPORTED_ON:
            return ts
        return base + (EXPORTED_ON - base) * self.rng.uniform(0.2, 0.8)

    def working_time_after(self, base: datetime, max_days: int) -> datetime:
        """A working-hours timestamp 0..max_days after base: people create CRM records during the day."""
        for _ in range(50):
            ts = at_local(local_date(base) + days(self.rng.randint(0, max_days)), self.rng.randint(450, 1080))
            if ts >= base + timedelta(minutes=20):
                return self.clamp(base, ts)
        return self.clamp(base, base + timedelta(minutes=30))

    def new_job(self, order: Order, units: int, created: datetime) -> Job:
        j = Job(self.guid(), order, units, created)
        order.jobs.append(j)
        return j

    def assign_coordinator(self, j: Job) -> None:
        miss = 0.17 if local_date(j.created).year == 2025 else 0.03    # field became mandatory practice in 2026
        j.coordinator = None if self.rng.random() < miss else \
            self.user[self.rng.choices(COORDINATORS, [30, 27, 23, 20])[0]]

    def assign_crew(self, j: Job, miss: float) -> None:
        rng = self.rng
        home = CITY_CREW.get(j.order.account.city)
        if rng.random() < miss:
            j.crew = None
        elif home and rng.random() < 0.8:
            j.crew = home
        else:
            j.crew = rng.choice(CREWS)

    def completed_units(self, units: int) -> int:
        r = self.rng.random()
        if r < 0.008:
            return 0                                              # crew did not fill it in
        if r < 0.022:
            return max(0, units - self.rng.randint(1, max(1, units // 5)))   # short: part of the order left
        if r < 0.040:
            return units + self.rng.randint(1, 3)                 # extra units added on site
        return units

    def spec_date(self, end_day: date) -> date | None:
        d = end_day + days(int(self.rng.triangular(0, 25, 4)))
        miss = (0.02 if end_day < date(2025, 7, 1) else 0.035 if end_day < date(2026, 1, 1)
                else 0.06 if end_day < date(2026, 7, 1) else 0.15)   # back office falls behind
        return None if d > REPORT_DATE or self.rng.random() < miss else d

    def schedule(self, j: Job, ps: date, pe: date, dur: int) -> None:
        """Plan the job, then let it run: its status is what the CRM shows at the export time."""
        rng = self.rng
        planned_at = at_local(ps - days(rng.randint(7, 35)), 600)
        if planned_at < j.created:
            planned_at = j.created + timedelta(hours=rng.uniform(1, 30))
        stuck = ps < REPORT_DATE - days(30) and rng.random() < 0.015     # never scheduled: forgotten
        if planned_at > EXPORTED_ON or stuck:
            j.status = "Not scheduled"
            self.assign_crew(j, 0.85)
            return
        j.planned_start = ps
        j.planned_end = None if rng.random() < 0.03 else pe
        self.assign_crew(j, 0.08)
        if ps < REPORT_DATE - days(20) and rng.random() < 0.012:    # crew never reported it: stays Scheduled
            j.status = "Scheduled"
            return
        r = rng.random()
        delay = (0 if r < 0.55 else rng.randint(1, 3) if r < 0.80 else rng.randint(4, 14) if r < 0.90
                 else rng.randint(15, 60))
        start_day = weekday_on_or_after(ps + days(delay))
        start = at_local(start_day, rng.randint(420, 570))
        r = rng.random()
        end_day = start_day + days(dur - 1 + (0 if r < 0.75 else 1 if r < 0.9 else rng.randint(2, 4)))
        end = at_local(end_day, rng.randint(840, 1110))
        if end <= EXPORTED_ON:
            j.status = "Completed"
            j.actual_start = None if rng.random() < 0.035 else start      # crew entered only the end
            j.actual_end = end
            j.installed_units = self.completed_units(j.units)
            j.spec_date = self.spec_date(end_day)
        elif start <= EXPORTED_ON:
            j.status = "In progress"
            j.actual_start = None if rng.random() < 0.05 else start
            j.installed_units = 0 if rng.random() < 0.4 else round(j.units * rng.uniform(0.2, 0.8))
        else:
            j.status = "Scheduled"

    def plan_jobs(self, o: Order, units: int) -> date | None:
        """Create the installation jobs of an order; returns the target install date of a signed order."""
        rng = self.rng
        if o.signed is None:      # the job is created with the order and waits for the signature
            j = self.new_job(o, units, self.working_time_after(o.created, 2))
            j.status = "Cancelled" if o.cancelled else "Not scheduled"
            self.assign_coordinator(j)
            self.assign_crew(j, 0.85)
            return None
        target = local_date(o.signed) + days(int(rng.triangular(10, 70, 25)))
        if target > date(2026, 12, 20):
            target = date(2026, 12, rng.randint(1, 18))
        if o.cancelled:
            j = self.new_job(o, units, self.working_time_after(o.signed, 3))
            j.status = "Cancelled"
            if rng.random() < 0.5:
                j.planned_start = weekday_on_or_after(target)
                j.planned_end = j.planned_start + days(rng.randint(0, 4))
            self.assign_coordinator(j)
            self.assign_crew(j, 0.3)
            return target
        if units >= 40 and rng.random() < 0.22:          # split installation: two visits
            first = round(units * rng.uniform(0.4, 0.7))
            parts = [first, units - first]
        else:
            parts = [units]
        created = self.working_time_after(o.signed, 3)
        prev_end = None
        for k, u in enumerate(parts):
            j = self.new_job(o, u, self.clamp(o.signed, created + timedelta(minutes=k * rng.randint(1, 20))))
            dur = min(12, max(1, math.ceil(u / rng.randint(10, 25))))
            ps = weekday_on_or_after(target + days(rng.randint(-4, 8)) if k == 0
                                     else prev_end + days(rng.randint(4, 21)))
            pe = ps + days(dur - 1)
            prev_end = pe
            self.assign_coordinator(j)
            self.schedule(j, ps, pe, dur)
        return target

    def make_order(self, created: datetime, account: Account) -> Order:
        rng = self.rng
        c_day = local_date(created)
        o = Order(self.guid(), account, self.order_owner(account, c_day), created)
        o.lines = self.make_lines()
        units = sum(l.qty for l in o.lines if l.product is not None and l.product.installable)
        o.cancelled = rng.random() < 0.042
        if rng.random() >= (0.55 if o.cancelled else 0.035):          # else never signed
            signed = at_local(c_day + days(int(rng.triangular(1, 45, 6))), rng.randint(480, 1050))
            o.signed = signed if signed <= EXPORTED_ON else None       # not signed yet at the export
        target = self.plan_jobs(o, units) if units > 0 else None

        owner_name = o.owner.name if o.owner else None
        if units > 0 and o.signed is not None and not o.cancelled:
            fill = PLANNED_MONTH_FILL.get(owner_name, 0.96)
            o.planned_month = month_start(target) if rng.random() < fill else None
        elif units > 0 and o.cancelled:
            o.planned_month = month_start(target) if target and rng.random() < 0.6 else None
        elif units > 0 and rng.random() < 0.3:                          # an estimate before signing
            o.planned_month = min(month_start(c_day + days(rng.randint(30, 75))), LAST_PLANNED_MONTH)

        # Installed in practice, but the signature date was never entered in the CRM
        if (not o.cancelled and o.signed is not None and o.jobs
                and all(j.status == "Completed" for j in o.jobs) and rng.random() < 0.015):
            o.signed = None
            o.tag = "installed, not signed in CRM"
        return o

    # ------------------------------------------------ deliberate edge cases on top of the random data

    def live_jobs(self) -> list[Job]:
        return [j for o in self.orders if not o.cancelled for j in o.jobs]

    def edge_installation_times(self) -> None:
        for kind, text in INSTALL_EDGES:
            x = parse_utc(text)
            xd = local_date(x)
            if kind == "end":
                cands = [j for j in self.live_jobs() if not j.tag and j.actual_start is not None
                         and j.actual_start <= x - timedelta(hours=6)
                         and ((j.status == "Completed" and abs((local_date(j.actual_end) - xd).days) <= 12)
                              or (j.status == "In progress" and x <= EXPORTED_ON))]
                check(bool(cands), f"no job to end at {text}")
                j = min(cands, key=lambda j: (abs(((local_date(j.actual_end) or xd) - xd).days),
                                              j.status != "Completed", j.id))
                if j.status == "In progress":
                    j.status, j.installed_units = "Completed", j.units
                j.actual_end = x
                if j.spec_date is not None and j.spec_date < xd:
                    j.spec_date = xd + days(self.rng.randint(1, 10))
                    if j.spec_date > REPORT_DATE:
                        j.spec_date = None
            elif kind == "start":
                cands = [j for j in self.live_jobs() if not j.tag and j.status == "Scheduled"
                         and REPORT_DATE - days(3) <= j.planned_start <= REPORT_DATE + days(7)]
                check(bool(cands), f"no job to start at {text}")
                j = min(cands, key=lambda j: (abs((j.planned_start - REPORT_DATE).days), j.id))
                j.status, j.actual_start, j.installed_units = "In progress", x, 0
            else:
                cands = [j for j in self.live_jobs() if not j.tag and j.status != "Cancelled"
                         and (j.order.signed or j.order.created) <= x - timedelta(hours=1)
                         and abs((j.created - x).total_seconds()) <= 5 * 86400
                         and (j.planned_start is None or j.planned_start > xd)
                         and (j.actual_start is None or j.actual_start > x)]
                check(bool(cands), f"no job to create at {text}")
                j = min(cands, key=lambda j: (abs((j.created - x).total_seconds()), j.id))
                j.created = x
            j.tag = f"edge {kind} {text}"

    def edge_in_progress(self) -> None:
        """Running jobs with part of the units already entered: they count only once the job has an end."""
        cands = sorted((j for j in self.live_jobs() if not j.tag and j.status == "Completed" and j.units >= 4
                        and j.actual_start is not None and local_date(j.actual_start) >= REPORT_DATE - days(12)),
                       key=lambda j: j.id)
        check(len(cands) >= 4, "not enough recent jobs for the in-progress edge case")
        for j in self.rng.sample(cands, 4):
            j.status, j.actual_end, j.spec_date = "In progress", None, None
            j.installed_units = max(1, round(j.units * self.rng.uniform(0.3, 0.7)))
            j.tag = "in progress, units entered"

    def edge_planned_starts(self) -> None:
        """Planned start on the Report Date is not a delay; the day before is."""
        for offset in (0, -1):
            cands = sorted((o.jobs[0] for o in self.orders if not o.cancelled and o.signed and len(o.jobs) == 1
                            and o.jobs[0].status == "Scheduled" and not o.jobs[0].tag
                            and o.jobs[0].planned_start > REPORT_DATE + days(2)), key=lambda j: j.id)
            check(bool(cands), "no scheduled job to move to the Report Date")
            j = self.rng.choice(cands)
            span = (j.planned_end - j.planned_start) if j.planned_end else None
            j.planned_start = REPORT_DATE + days(offset)
            j.planned_end = j.planned_start + span if span is not None else None
            j.tag = f"edge planned start R{offset:+d}"

    def cancelled_copy(self, j: Job, tag: str, created: datetime) -> Job:
        c = copy.copy(j)
        c.id, c.status, c.tag, c.created = self.guid(), "Cancelled", tag, created
        j.order.jobs.append(c)
        return c

    def edge_cancelled_jobs(self) -> None:
        rng = self.rng
        by_id = lambda j: j.id
        # Duplicates cancelled instead of deleted: the copied units and dates must not count twice
        cands = sorted((j for j in self.live_jobs() if not j.tag and j.status == "Completed"
                        and j.installed_units > 0), key=by_id)
        for j in rng.sample(cands, 5):
            self.cancelled_copy(j, "duplicate, cancelled", min(j.created + timedelta(minutes=rng.randint(3, 40)),
                                                               EXPORTED_ON))
            j.tag = "has cancelled duplicate"
        # A cancelled duplicate that ended later than the live job of an installed order: the Installed Date
        # (and Avg Days Signed to Installed) must come from the live job
        def fully_installed(o: Order) -> bool:
            live = [x for x in o.jobs if x.status != "Cancelled"]
            units = sum(l.qty for l in o.lines if l.product is not None and l.product.installable)
            return (bool(live) and all(x.status == "Completed" for x in live)
                    and sum(x.installed_units for x in live) >= units)
        cands = sorted((j for j in self.live_jobs() if not j.tag and j.status == "Completed" and j.installed_units > 0
                        and j.order.signed is not None and j.actual_end <= EXPORTED_ON - days(10)
                        and fully_installed(j.order)
                        and j.actual_end == max(x.actual_end for x in j.order.jobs if x.status != "Cancelled")),
                       key=by_id)
        for j in rng.sample(cands, 2):
            c = self.cancelled_copy(j, "duplicate, cancelled, later end",
                                    min(j.created + timedelta(minutes=rng.randint(3, 40)), EXPORTED_ON))
            c.actual_end = j.actual_end + days(rng.randint(2, 6))
            c.spec_date = None
            j.tag = "has cancelled duplicate with a later end"
        # A visit that started and was abandoned (cancelled); the work is re-planned for later.
        # A started cancelled job must not make the order "In progress": it stays "Scheduled"
        cands = sorted((o.jobs[0] for o in self.orders if not o.cancelled and o.signed and len(o.jobs) == 1
                        and o.jobs[0].status == "Scheduled" and not o.jobs[0].tag
                        and o.jobs[0].planned_start > REPORT_DATE + days(2)
                        and local_date(o.jobs[0].created) <= REPORT_DATE - days(25)), key=by_id)
        check(len(cands) >= 2, "not enough scheduled jobs for the started-and-cancelled edge case")
        for j in rng.sample(cands, 2):
            start_day = REPORT_DATE - days(rng.randint(8, 20))
            start_day -= days(max(0, start_day.weekday() - 4))           # a working day
            c = self.cancelled_copy(j, "cancelled visit, started", j.created)
            c.planned_start, c.planned_end = start_day, start_day + days(1)
            c.actual_start, c.actual_end = at_local(start_day, rng.randint(450, 570)), None
            c.installed_units, c.spec_date = 0, None
            c.crew = c.crew or rng.choice(CREWS)
            j.tag = "re-planned after a started visit"
        # Rescheduled: the first job was cancelled and replaced; its earlier planned start must be ignored
        cands = sorted((o.jobs[0] for o in self.orders if not o.cancelled and len(o.jobs) == 1
                        and o.jobs[0].status in ("Scheduled", "Completed") and not o.jobs[0].tag), key=by_id)
        for j in rng.sample(cands, 6):
            c = self.cancelled_copy(j, "replaced, cancelled", j.created)
            c.actual_start = c.actual_end = c.spec_date = None
            c.installed_units = 0
            c.planned_start = weekday_on_or_after(j.planned_start - days(rng.randint(7, 21)))
            c.planned_end = c.planned_start + days(rng.randint(0, 3))
            j.tag = "replacement"
        # Not scheduled again after a cancelled visit with a past planned start: "To schedule", not "Delayed start"
        cands = sorted((o for o in self.orders if not o.cancelled and o.signed and len(o.jobs) == 1
                        and o.jobs[0].status == "Not scheduled" and not o.jobs[0].tag
                        and local_date(o.signed) <= REPORT_DATE - days(14)), key=lambda o: o.id)
        for o in rng.sample(cands, 3):
            j = o.jobs[0]
            c = self.cancelled_copy(j, "cancelled visit, past planned start", j.created)
            c.planned_start = max(local_date(o.signed) + days(2), REPORT_DATE - days(rng.randint(2, 10)))
            c.planned_end = c.planned_start + days(1)
            c.crew = c.crew or rng.choice(CREWS)
            j.tag = "re-planning"
        # The only job was cancelled and not recreated yet: no live job, so "To schedule"
        delayed = sorted((o.jobs[0] for o in self.orders if not o.cancelled and len(o.jobs) == 1
                          and o.jobs[0].status == "Scheduled" and not o.jobs[0].tag
                          and o.jobs[0].planned_start < REPORT_DATE), key=by_id)
        future = sorted((o.jobs[0] for o in self.orders if not o.cancelled and len(o.jobs) == 1
                         and o.jobs[0].status == "Scheduled" and not o.jobs[0].tag
                         and o.jobs[0].planned_start > REPORT_DATE), key=by_id)
        check(len(delayed) >= 2 and len(future) >= 1, "not enough scheduled jobs for the cancelled-only edge case")
        for j in rng.sample(delayed, 2) + rng.sample(future, 1):
            j.status, j.tag = "Cancelled", "only job cancelled"

    def edge_cancelled_orders_with_live_jobs(self) -> None:
        """Order cancelled in the CRM while its job stays live: Dataverse does not cascade the cancellation
        to rows of a custom table. Jobs and the installation DQ checks still count such a job;
        Units Installed and the order status do not."""
        rng = self.rng
        cases = [("Completed", lambda j: j.installed_units > 0), ("Scheduled", lambda j: j.crew is not None),
                 ("Not scheduled", lambda j: True), ("In progress", lambda j: True)]
        for status, extra in cases:
            cands = sorted((o for o in self.orders if not o.cancelled and not o.tag and len(o.jobs) == 1
                            and (o.signed is not None or status == "Not scheduled")
                            and not o.jobs[0].tag and o.jobs[0].status == status and extra(o.jobs[0])),
                           key=lambda o: o.id)
            check(bool(cands), f"no order with a {status} job to cancel")
            o = rng.choice(cands)
            o.cancelled, o.tag = True, "cancelled, job still live"
            o.jobs[0].tag = "live job of a cancelled order"

    def edge_signed_times(self) -> None:
        """Signed just after local midnight: the UTC timestamp is on the previous day (and month)."""
        rng = self.rng
        cands = sorted((o for o in self.orders if o.signed and not o.tag
                        and local_date(o.signed) > local_date(o.created)), key=lambda o: o.id)
        firsts = [o for o in cands if local_date(o.signed).day == 1]
        picks = rng.sample(firsts, 4)
        picks += rng.sample([o for o in cands if o not in picks], 6)
        for o in picks:
            new = at_local(local_date(o.signed), rng.randint(5, 55))
            if new >= o.created + timedelta(hours=1):
                o.signed, o.tag = new, "signed after midnight"

    def edge_multiline_texts(self) -> None:
        """Multi-line text, as users type it into a CRM memo field: the CSV quotes the value and keeps the
        line break inside it. Only RFC 4180 quoting (QuoteStyle.Csv) reads such a line as one row.
        Picked without the random generator, so the rest of the data does not change."""
        notes = ["staircase B, 2nd floor", "facade, east side", "as agreed with the property manager"]
        lines = sorted((l for o in self.orders if not o.cancelled for l in o.lines if l.product is None),
                       key=lambda l: l.id)
        for line, note in zip(lines, notes):
            line.description += "\n" + note

    def finish(self) -> None:
        rng = self.rng
        for o in self.orders:
            units = sum(l.qty for l in o.lines if l.product is not None and l.product.installable)
            live = [j for j in o.jobs if j.status != "Cancelled"]
            if o.cancelled:
                o.statecode = 2
            elif o.signed is None:
                o.statecode = 3 if o.tag == "installed, not signed in CRM" else 0
            elif units == 0:
                o.statecode = 3 if local_date(o.signed) < REPORT_DATE - days(30) else 1
            elif live and all(j.status == "Completed" for j in live):
                o.statecode = 3 if rng.random() < 0.9 else 1    # back office closes most of them
            else:
                o.statecode = 1
        self.orders.sort(key=lambda o: (o.created, o.id))
        seq = Counter()
        for o in self.orders:
            y = local_date(o.created).year
            seq[y] += 1
            o.number = f"ORD-{y % 100:02d}-{seq[y]:05d}"
        self.jobs = sorted((j for o in self.orders for j in o.jobs), key=lambda j: (j.created, j.id))
        seq = Counter()
        for j in self.jobs:
            y = local_date(j.created).year
            seq[y] += 1
            j.name = f"INS-{y % 100:02d}-{seq[y]:05d}"
        for o in self.orders:
            o.jobs.sort(key=lambda j: j.name)


# ---------------------------------------------------------------- self-checks on the truth

def utc_shifted(ts: datetime | None) -> bool:
    return ts is not None and ts.astimezone(UTC).date() != local_date(ts)


# Wrong time-zone rules a model could implement. Each gives the summer-time period of a year as
# (first day, day after the last day), switching at 01:00 UTC like the EU rule; None = no summer time.
# They only measure whether the data can tell such a rule from the right one; the expected
# figures use zoneinfo.
def last_sunday(y: int, m: int) -> date:
    d = month_end(date(y, m, 1))
    return d - days((d.weekday() + 1) % 7)


def nth_sunday(y: int, m: int, n: int) -> date:
    d = date(y, m, 1)
    return d + days((6 - d.weekday()) % 7 + 7 * (n - 1))


EU_RULE = lambda y: (last_sunday(y, 3), last_sunday(y, 10))
WRONG_RULES = {
    "fixed_utc_plus_1": None,
    "fixed_utc_plus_2": lambda y: (date(y - 1, 12, 31), date(y + 1, 1, 1)),
    "us_dst_dates": lambda y: (nth_sunday(y, 3, 2), nth_sunday(y, 11, 1)),
    "summer_starts_7_days_early": lambda y: (last_sunday(y, 3) - days(7), last_sunday(y, 10)),
    "summer_starts_7_days_late": lambda y: (last_sunday(y, 3) + days(7), last_sunday(y, 10)),
    "summer_ends_7_days_early": lambda y: (last_sunday(y, 3), last_sunday(y, 10) - days(7)),
    "summer_ends_7_days_late": lambda y: (last_sunday(y, 3), last_sunday(y, 10) + days(7)),
    "summer_1_mar_to_30_nov": lambda y: (date(y, 3, 1), date(y, 12, 1)),
}
# Rules where an order or a job end can change month in this date range (a switch a week early or
# late in spring, or early in autumn, never crosses a month end)
MONTH_MOVE_RULES = ["fixed_utc_plus_1", "fixed_utc_plus_2", "us_dst_dates", "summer_starts_7_days_late",
                    "summer_ends_7_days_late", "summer_1_mar_to_30_nov"]


def rule_local_date(ts: datetime, rule) -> date:
    u = ts.astimezone(UTC).replace(tzinfo=None)
    summer = False
    if rule is not None:
        first, after = rule(u.year)
        summer = datetime(first.year, first.month, first.day, 1) <= u < datetime(after.year, after.month, after.day, 1)
    return (u + timedelta(hours=2 if summer else 1)).date()


def month_moves(g: "Generator", rule) -> int:
    stamps = [o.created for o in g.orders] + [j.actual_end for j in g.jobs if j.actual_end]
    return sum(month_start(rule_local_date(t, rule)) != month_start(local_date(t)) for t in stamps)


def check_truth(g: Generator) -> None:
    for o in g.orders:
        c = local_date(o.created)
        check(FIRST_ORDER_DAY <= c <= LAST_ORDER_DAY, f"{o.number}: created {c} outside the order range")
        check(1 <= len(o.lines) <= 6, f"{o.number}: {len(o.lines)} lines")
        for l in o.lines:
            check(l.amount == (l.qty * l.price - l.discount).quantize(CENT), f"{o.number}: line amount")
            check((l.product is None) == (l.description is not None), f"{o.number}: write-in flag")
        check(o.total == sum(l.amount for l in o.lines), f"{o.number}: totalamount")
        if o.signed is not None:
            check(o.created + timedelta(hours=1) <= o.signed <= EXPORTED_ON, f"{o.number}: signed time")
        if o.planned_month is not None:
            check(o.planned_month.day == 1 and DATE_TABLE[0] <= o.planned_month <= DATE_TABLE[1],
                  f"{o.number}: planned month {o.planned_month}")
        units = sum(l.qty for l in o.lines if l.product is not None and l.product.installable)
        check((units > 0) == bool(o.jobs), f"{o.number}: jobs exist exactly when there is something to install")
        check((o.statecode == 2) == o.cancelled, f"{o.number}: statecode")
    for j in g.jobs:
        n = j.name
        for ts in (j.created, j.actual_start, j.actual_end):
            check(ts is None or ts <= EXPORTED_ON, f"{n}: timestamp after the export")
        check(j.created >= j.order.created, f"{n}: created before its order")
        check(j.installed_units >= 0, f"{n}: negative units")
        if j.planned_start and j.planned_end:
            check(j.planned_end >= j.planned_start, f"{n}: planned end before planned start")
        if j.status == "Completed":
            check(j.actual_end is not None and j.planned_start is not None, f"{n}: completed without end or plan")
            check(j.actual_start is None or j.actual_start <= j.actual_end, f"{n}: end before start")
        elif j.status == "In progress":
            check(j.actual_end is None and j.planned_start is not None, f"{n}: in progress with an end")
        elif j.status == "Scheduled":
            check(j.planned_start is not None and j.actual_start is None and j.actual_end is None,
                  f"{n}: scheduled without a plan or with actuals")
        elif j.status == "Not scheduled":
            check(j.planned_start is None and j.actual_start is None and j.actual_end is None,
                  f"{n}: not scheduled with a plan or actuals")
        if j.spec_date is not None:     # cancelled duplicates keep the copied specification date
            check(j.status in ("Completed", "Cancelled") and j.actual_end is not None
                  and local_date(j.actual_end) <= j.spec_date <= REPORT_DATE, f"{n}: specification date")
        if j.coordinator is not None:
            check(j.coordinator.department == "Installation", f"{n}: coordinator is not an Installation user")
    for collection, label in ((g.orders, "order"), (g.jobs, "job"), (g.accounts, "account")):
        ids = [x.id for x in collection]
        check(len(ids) == len(set(ids)), f"duplicate {label} id")
    check(len({o.number for o in g.orders}) == len(g.orders), "duplicate order number")
    check(len({j.name for j in g.jobs}) == len(g.jobs), "duplicate installation number")
    check(len({a.name for a in g.accounts}) == len(g.accounts), "duplicate account name")
    check(sum(1 for a in g.accounts if "," in a.name or '"' in a.name) >= 3, "names that need CSV quoting")
    check(sum(1 for o in g.orders for l in o.lines if l.description and "\n" in l.description) >= 3,
          "fewer than 3 order lines with a line break in the text")

    # Edge-case minimums of the spec
    o_shift = [o for o in g.orders if utc_shifted(o.created)]
    check(len(o_shift) >= 12, f"only {len(o_shift)} orders with a UTC/local date shift")
    check(sum(1 for o in o_shift if o.created.astimezone(UTC).month != local_date(o.created).month) >= 3,
          "fewer than 3 orders on a month boundary")
    check(sum(1 for o in o_shift if o.created.astimezone(UTC).year == 2025 and local_date(o.created).year == 2026)
          >= 2, "fewer than 2 orders on the 2025/2026 boundary")
    check(sum(1 for j in g.jobs if any(utc_shifted(t) for t in (j.created, j.actual_start, j.actual_end))) >= 6,
          "fewer than 6 installations with a date shift")
    check(sum(1 for a in g.accounts if utc_shifted(a.created)) >= 6, "fewer than 6 accounts with a date shift")
    for d in DST_DAYS:
        check(any(local_date(t) == d and utc_shifted(t) for t in all_timestamps(g)),
              f"no shifted record on the DST switch day {d}")
    stamps = all_timestamps(g)
    # The harness for the wrong rules below must give zoneinfo's answer when it is given the right rule
    check(all(rule_local_date(t, EU_RULE) == local_date(t) for t in stamps), "EU rule harness differs from zoneinfo")
    for name in WRONG_RULES:
        check(any(rule_local_date(t, WRONG_RULES[name]) != local_date(t) for t in stamps),
              f"no record whose local date changes under the wrong rule {name}")
    for name in MONTH_MOVE_RULES:
        check(month_moves(g, WRONG_RULES[name]) > 0, f"no order or job end changes month under the wrong rule {name}")

    # Record types the model has special rows or rules for
    live = lambda o: [j for j in o.jobs if j.status != "Cancelled"]
    check(sum(l.product is None for o in g.orders for l in o.lines) >= 10, "fewer than 10 write-in lines")
    check(sum(o.owner is None for o in g.orders) >= 10, "fewer than 10 orders without an owner")
    check(any(o.owner is not None and o.owner.disabled for o in g.orders), "no order of a disabled user")
    check(sum(len(live(o)) == 2 for o in g.orders) >= 10, "fewer than 10 split installations")
    check(any(j.status == "Completed" and j.actual_start is None for j in g.jobs), "no completed job without a start")
    check(any(j.status == "Cancelled" and j.actual_end is not None for j in g.jobs), "no cancelled duplicate of a done job")

    # Job edge cases that only a wrong job filter would get wrong
    on_cancelled = [j for o in g.orders if o.cancelled for j in live(o)]
    check(any(j.status == "Completed" and j.installed_units > 0 for j in on_cancelled),
          "no completed job with installed units on a cancelled order")
    check(any(j.status == "Scheduled" and j.crew for j in on_cancelled), "no scheduled job on a cancelled order")
    check(any(j.status == "Not scheduled" for j in on_cancelled), "no unscheduled job on a cancelled order")
    started_cancelled = [o for o in g.orders if not o.cancelled
                         and any(j.status == "Cancelled" and j.actual_start for j in o.jobs)
                         and not any(j.actual_start or j.actual_end for j in live(o))]
    check(len(started_cancelled) >= 2, "fewer than 2 orders with only a cancelled job started")
    later_end = [o for o in g.orders if not o.cancelled and live(o) and all(j.actual_end for j in live(o))
                 and any(j.status == "Cancelled" and j.actual_end and j.actual_end > max(x.actual_end for x in live(o))
                         for j in o.jobs)]
    check(len(later_end) >= 2, "fewer than 2 orders with a cancelled job that ended after the live jobs")


def all_timestamps(g: Generator) -> list[datetime]:
    out = [a.created for a in g.accounts]
    out += [t for o in g.orders for t in (o.created, o.signed) if t]
    out += [t for j in g.jobs for t in (j.created, j.actual_start, j.actual_end) if t]
    return out


# ---------------------------------------------------------------- CSV output

def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, datetime):
        return utc_text(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return f"{v:.2f}"
    return str(v)


def table_rows(g: Generator) -> dict[str, tuple[list[str], list[list]]]:
    return {
        "export_info.csv": (["exportedon", "source"], [[EXPORTED_ON, SOURCE]]),
        "systemuser.csv": (["systemuserid", "fullname", "ops_department", "isdisabled"],
                           [[u.id, u.name, u.department, u.disabled] for u in g.users]),
        "account.csv": (["accountid", "name", "address1_city", "ops_segment", "ownerid", "createdon"],
                        [[a.id, a.name, a.city, a.segment, a.owner.id, a.created] for a in g.accounts]),
        "product.csv": (["productid", "productnumber", "name", "ops_productgroup", "ops_isinstallable", "price"],
                        [[p.id, p.number, p.name, p.group, p.installable, p.price] for p in g.products]),
        "salesorder.csv": (["salesorderid", "ordernumber", "customerid", "ownerid", "createdon", "ops_signedon",
                            "ops_plannedinstallmonth", "statecode", "statecodename", "totalamount"],
                           [[o.id, o.number, o.account.id, o.owner.id if o.owner else None, o.created, o.signed,
                             o.planned_month, o.statecode, STATECODES[o.statecode], o.total] for o in g.orders]),
        "salesorderdetail.csv": (["salesorderdetailid", "salesorderid", "productid", "isproductoverridden",
                                  "productdescription", "quantity", "priceperunit", "manualdiscountamount",
                                  "extendedamount"],
                                 [[l.id, o.id, l.product.id if l.product else None, l.product is None,
                                   l.description, l.qty, l.price, l.discount, l.amount]
                                  for o in g.orders for l in o.lines]),
        "ops_installation.csv": (["ops_installationid", "ops_name", "ops_salesorderid", "ops_crew",
                                  "ops_coordinatorid", "ops_plannedstart", "ops_plannedend", "ops_actualstart",
                                  "ops_actualend", "ops_installedunits", "statuscode", "statuscodename",
                                  "ops_specificationdate", "createdon"],
                                 [[j.id, j.name, j.order.id, j.crew, j.coordinator.id if j.coordinator else None,
                                   j.planned_start, j.planned_end, j.actual_start, j.actual_end, j.installed_units,
                                   JOB_STATUS[j.status], j.status, j.spec_date, j.created] for j in g.jobs]),
    }


def write_csvs(tables: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for name, (header, rows) in tables.items():
        # UTF-8 without BOM, CRLF and quotes only where needed (RFC 4180)
        with open(DATA / name, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\r\n")
            w.writerow(header)
            w.writerows([[fmt(v) for v in row] for row in rows])


def read_back(tables: dict) -> None:
    """Self-check only: the files on disk parse back to exactly what was meant to be written."""
    parsed = {}
    for name, (header, rows) in tables.items():
        raw = (DATA / name).read_bytes()
        check(not raw.startswith(b"\xef\xbb\xbf"), f"{name}: has a BOM")
        with open(DATA / name, encoding="utf-8", newline="") as f:
            got = list(csv.reader(f))
        check(got[0] == header, f"{name}: header")
        check(got[1:] == [[fmt(v) for v in row] for row in rows], f"{name}: content does not read back")
        parsed[name] = [dict(zip(header, r)) for r in got[1:]]
    sums = defaultdict(Decimal)
    for r in parsed["salesorderdetail.csv"]:
        amount = Decimal(r["extendedamount"])
        check(amount == Decimal(r["quantity"]) * Decimal(r["priceperunit"]) - Decimal(r["manualdiscountamount"]),
              "extendedamount in the CSV")
        sums[r["salesorderid"]] += amount
    for r in parsed["salesorder.csv"]:
        check(Decimal(r["totalamount"]) == sums[r["salesorderid"]], f"{r['ordernumber']}: totalamount in the CSV")


# ---------------------------------------------------------------- expected figures (independent rules)

@dataclass
class OrderFacts:
    number: str
    customer: str
    owner: str
    segment: str
    created: date
    signed: date | None
    planned_month: date | None
    cancelled: bool
    crm_status: str
    lines: list[tuple[str, bool, int, Decimal]]    # product group, installable, quantity, amount
    jobs: list[tuple[str, date | None, int]]        # live jobs: crew, local actual end date, installed units
    installable_units: int
    installed_units: int
    status: str
    installed_date: date | None
    earliest_planned_start: date | None


def order_facts(o: Order) -> OrderFacts:
    R = REPORT_DATE
    lines = [(l.product.group if l.product else WRITE_IN_GROUP, bool(l.product and l.product.installable),
              l.qty, l.amount) for l in o.lines]
    installable = sum(q for _, inst, q, _ in lines if inst)
    live = [j for j in o.jobs if j.status != "Cancelled"]
    ends = [local_date(j.actual_end) for j in live]
    installed = sum(j.installed_units for j, e in zip(live, ends) if e is not None and e <= R)
    # A job has started when it has an actual start, or an actual end only (the crew entered just the end)
    starts = [local_date(j.actual_start or j.actual_end) for j in live if (j.actual_start or j.actual_end) is not None]
    planned = [j.planned_start for j in live if j.planned_start is not None]
    cancelled = o.statecode == 2
    if cancelled:
        status = "Cancelled"
    elif installable == 0:
        status = "No installation"
    elif installed >= installable:
        status = "Installed"
    elif any(s <= R for s in starts):
        status = "In progress"
    elif not planned:
        status = "To schedule"
    elif min(planned) < R:
        status = "Delayed start"
    else:
        status = "Scheduled"
    done = [e for e in ends if e is not None and e <= R]
    return OrderFacts(
        number=o.number, customer=o.account.name, owner=o.owner.name if o.owner else NO_OWNER,
        segment=o.account.segment or NOT_SET, created=local_date(o.created), signed=local_date(o.signed),
        planned_month=o.planned_month, cancelled=cancelled, crm_status=STATECODES[o.statecode], lines=lines,
        jobs=[(j.crew or NOT_ASSIGNED, e, j.installed_units) for j, e in zip(live, ends)],
        installable_units=installable, installed_units=installed, status=status,
        installed_date=max(done) if status == "Installed" else None,
        earliest_planned_start=min(planned) if planned else None)


def dq_checks(g: Generator, facts: dict[str, OrderFacts]) -> list[tuple[int, str, date, int]]:
    """One row per applicable record and field: (field key, owner, local created date, is filled)."""
    rows = []
    for a in g.accounts:
        d = local_date(a.created)
        rows.append((1, a.owner.name, d, int(a.city is not None)))
        rows.append((2, a.owner.name, d, int(a.segment is not None)))
    for o in g.orders:
        f = facts[o.id]
        if not f.cancelled:
            rows.append((3, f.owner, f.created, int(o.owner is not None)))
            if f.signed is not None and f.installable_units > 0:
                rows.append((4, f.owner, f.created, int(o.planned_month is not None)))
            rows += [(5, f.owner, f.created, int(l.product is not None)) for l in o.lines]
        # The installation checks depend on the job status only: a live job of a cancelled order is checked too
        for j in o.jobs:
            if j.status == "Cancelled":
                continue
            d = local_date(j.created)
            if j.status in ("Scheduled", "In progress", "Completed"):
                rows.append((6, f.owner, d, int(j.crew is not None)))
            rows.append((7, f.owner, d, int(j.coordinator is not None)))
            if j.status in ("In progress", "Completed"):
                rows.append((8, f.owner, d, int(j.actual_start is not None)))
            if j.status == "Completed":
                rows.append((9, f.owner, d, int(j.spec_date is not None)))
    return rows


@dataclass(frozen=True)
class Ctx:
    """A filter context: what a DAX query would put on the dimension columns."""
    dates: tuple[date, date] | None = None    # 'Date'[Date] range
    owner: str | None = None                  # 'Owner'[Owner]
    segment: str | None = None                # 'Customer'[Segment]
    group: str | None = None                  # 'Product'[Product Group]
    status: str | None = None                 # 'Orders'[Installation Status]
    crew: str | None = None                   # 'Crew'[Crew]
    dq_field: str | None = None               # 'DQ Fields'[Field]
    dept: str | None = None                   # 'DQ Fields'[Responsible Department]


def ratio(a, b) -> float | None:
    return float(a) / float(b) if b else None


def money(v: Decimal) -> float:
    return float(v.quantize(CENT))


def in_dates(d: date | None, dates: tuple[date, date] | None) -> bool:
    return dates is None or (d is not None and dates[0] <= d <= dates[1])


class Calc:
    """The measures of the spec over the truth records, one filter context at a time."""

    def __init__(self, g: Generator):
        facts = {o.id: order_facts(o) for o in g.orders}
        self.facts = list(facts.values())
        self.checks = dq_checks(g, facts)
        self.field_name = {k: f for k, f, _, _ in DQ_FIELDS}
        self.field_dept = {f: d for _, f, _, d in DQ_FIELDS}

    def orders(self, ctx: Ctx, by: str | None = "created", cancelled: bool = False):
        """Orders in the context. by = the date column the Date filter goes through (None = removed);
        signed / planned_month also drop orders where that date is empty (the measures count only those)."""
        for f in self.facts:
            if f.cancelled != cancelled:
                continue
            if ctx.owner is not None and f.owner != ctx.owner:
                continue
            if ctx.segment is not None and f.segment != ctx.segment:
                continue
            if ctx.status is not None and f.status != ctx.status:
                continue
            if by in ("signed", "planned_month") and getattr(f, by) is None:
                continue
            if by is not None and not in_dates(getattr(f, by), ctx.dates):
                continue
            yield f

    def value(self, ctx: Ctx, by: str = "created") -> Decimal:
        return sum((amt for f in self.orders(ctx, by) for g, _, _, amt in f.lines
                    if ctx.group is None or g == ctx.group), Decimal(0))

    def units(self, ctx: Ctx, by: str = "created") -> int:
        return sum(q for f in self.orders(ctx, by) for g, inst, q, _ in f.lines
                   if inst and (ctx.group is None or g == ctx.group))

    @staticmethod
    def ytd_window(ctx: Ctx) -> tuple[date, date]:
        last = ctx.dates[1] if ctx.dates else DATE_TABLE[1]
        return date(last.year, 1, 1), min(last, REPORT_DATE)

    def units_installed(self, ctx: Ctx) -> int:
        # REMOVEFILTERS('Date') + TREATAS on the local Actual End Date; owner/segment/status still apply
        return sum(u for f in self.orders(ctx, by=None) for crew, end, u in f.jobs
                   if end is not None and end <= REPORT_DATE and in_dates(end, ctx.dates)
                   and (ctx.crew is None or crew == ctx.crew))

    def dq(self, ctx: Ctx) -> tuple[int, int]:
        n = filled = 0
        for key, owner, d, ok in self.checks:
            name = self.field_name[key]
            if ctx.dq_field is not None and name != ctx.dq_field:
                continue
            if ctx.dept is not None and self.field_dept[name] != ctx.dept:
                continue
            if ctx.owner is not None and owner != ctx.owner:
                continue
            if not in_dates(d, ctx.dates):
                continue
            n += 1
            filled += ok
        return n, filled

    def fields_below_target(self, ctx: Ctx) -> int:
        count = 0
        for _, name, _, dept in DQ_FIELDS:
            if ctx.dept is not None and dept != ctx.dept:
                continue
            n, filled = self.dq(replace(ctx, dq_field=name))
            check(n > 0, f"Fields Below Target asked in a context where {name} has no checks")
            count += filled / n < TARGET
        return count

    def measure(self, name: str, ctx: Ctx = Ctx()):
        R = REPORT_DATE
        orders = lambda **kw: list(self.orders(ctx, **kw))
        if name == "Orders":
            return len(orders())
        if name == "Cancelled Orders":
            return len(orders(cancelled=True))
        if name == "Order Value":
            return money(self.value(ctx))
        if name == "Average Order Value":
            return ratio(self.value(ctx), len(orders()))
        if name == "Units Ordered":
            return self.units(ctx)
        if name == "Signed Orders":
            return len(orders(by="signed"))
        if name == "Signed Value":
            return money(self.value(ctx, "signed"))
        if name == "Unsigned Orders":
            return sum(1 for f in orders() if f.signed is None)
        if name == "Order Value PY":
            d0, d1 = ctx.dates or DATE_TABLE
            return money(self.value(replace(ctx, dates=(shift_year(d0), shift_year(d1)))))
        if name == "Order Value YTD":
            return money(self.value(replace(ctx, dates=self.ytd_window(ctx))))
        if name == "Order Value PY YTD":
            d0, d1 = self.ytd_window(ctx)
            return money(self.value(replace(ctx, dates=(shift_year(d0), shift_year(d1)))))
        if name == "YTD Growth %":
            ytd, py = self.measure("Order Value YTD", ctx), self.measure("Order Value PY YTD", ctx)
            return ratio(Decimal(str(ytd)) - Decimal(str(py)), py)
        if name == "Units Planned":
            return self.units(ctx, "planned_month")
        if name == "Units Installed":
            return self.units_installed(ctx)
        if name == "Installation Rate %":
            return ratio(self.units_installed(ctx), self.units(ctx, "planned_month"))
        if name == "Open Orders":
            return sum(1 for f in orders() if f.installable_units > 0 and f.status != "Installed")
        if name == "Delayed Starts":
            return sum(1 for f in orders() if f.status == "Delayed start")
        if name == "Orders Installed":
            return sum(1 for f in orders() if f.status == "Installed")
        if name == "Avg Days Signed to Installed":
            spans = [(f.installed_date - f.signed).days for f in orders()
                     if f.status == "Installed" and f.signed is not None]
            return ratio(sum(spans), len(spans))
        if name == "Units Backlog":
            return sum(f.installable_units - f.installed_units for f in orders()
                       if f.signed is not None and f.status != "Installed")
        if name == "Orders Without Owner":
            return sum(1 for f in orders() if f.owner == NO_OWNER)
        if name == "Jobs":
            # Non-cancelled jobs, also those of cancelled orders (the spec filters on the job status only)
            return sum(1 for f in orders() + orders(cancelled=True) for crew, _, _ in f.jobs
                       if ctx.crew is None or crew == ctx.crew)
        if name == "Checks":
            return self.dq(ctx)[0]
        if name == "Filled Values":
            return self.dq(ctx)[1]
        if name == "Missing Values":
            n, filled = self.dq(ctx)
            return n - filled
        if name == "Completeness %":
            n, filled = self.dq(ctx)
            return ratio(filled, n)
        if name == "Target Completeness":
            return TARGET
        if name == "Fields Below Target":
            return self.fields_below_target(ctx)
        if name == "Report Date":
            return R.isoformat()
        raise KeyError(name)

    def block(self, names: list[str], ctx: Ctx) -> dict:
        return {m: self.measure(m, ctx) for m in names}


SALES_MEASURES = ["Orders", "Cancelled Orders", "Order Value", "Average Order Value", "Units Ordered",
                  "Signed Orders", "Signed Value", "Unsigned Orders", "Order Value PY", "Order Value YTD",
                  "Order Value PY YTD", "YTD Growth %"]
OPS_MEASURES = ["Units Planned", "Units Installed", "Installation Rate %", "Open Orders", "Delayed Starts",
                "Orders Installed", "Avg Days Signed to Installed", "Units Backlog", "Orders Without Owner", "Jobs"]
DQ_MEASURES = ["Checks", "Filled Values", "Missing Values", "Completeness %"]
ALL_MEASURES = (SALES_MEASURES + OPS_MEASURES + DQ_MEASURES + ["Target Completeness", "Fields Below Target"]
                + ["Report Date"])
MONTH_MEASURES = ["Orders", "Cancelled Orders", "Order Value", "Average Order Value", "Units Ordered",
                  "Signed Orders", "Signed Value", "Order Value PY", "Order Value YTD", "Order Value PY YTD",
                  "YTD Growth %", "Units Planned", "Units Installed", "Installation Rate %"]
OWNER_MEASURES = ["Orders", "Cancelled Orders", "Order Value", "Signed Value", "Unsigned Orders", "Open Orders",
                  "Delayed Starts", "Units Installed", "Checks", "Filled Values", "Missing Values", "Completeness %"]
# Two filters at once, like the Year and Owner slicers of the Sales page
YEAR_OWNER_MEASURES = SALES_MEASURES + ["Units Installed"]
MONTH_OWNER_MEASURES = ["Orders", "Order Value", "Signed Orders", "Signed Value", "Order Value PY", "Order Value YTD",
                        "YTD Growth %", "Units Planned", "Units Installed"]
MONTH_SEGMENT_MEASURES = ["Orders", "Order Value", "Units Ordered", "Signed Value", "Units Installed"]
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONEY_MEASURES = ["Order Value", "Average Order Value", "Signed Value", "Order Value PY", "Order Value YTD",
                  "Order Value PY YTD"]
RATE_MEASURES = ["YTD Growth %", "Installation Rate %", "Completeness %", "Target Completeness"]


def utc_checks(g: Generator, calc: Calc) -> dict:
    """How many records a missing or wrong UTC conversion would move; all must be > 0 for the test to bite."""
    moved = [o for o in g.orders if month_start(o.created.astimezone(UTC).date()) != month_start(local_date(o.created))]
    live_value = sum((o.total for o in moved if not o.cancelled), Decimal(0))
    stamps = all_timestamps(g)
    return {
        "orders_created_date_differs": sum(utc_shifted(o.created) for o in g.orders),
        "orders_move_month_if_utc_dates_used": len(moved),
        "orders_move_year_if_utc_dates_used": sum(o.created.astimezone(UTC).year != local_date(o.created).year
                                                  for o in g.orders),
        "order_value_moving_month_if_utc_dates_used": money(live_value),
        "orders_signed_date_differs": sum(utc_shifted(o.signed) for o in g.orders),
        "orders_signed_month_differs": sum(o.signed is not None and o.signed.astimezone(UTC).month
                                           != local_date(o.signed).month for o in g.orders),
        "installations_with_a_date_shift": sum(any(utc_shifted(t) for t in (j.created, j.actual_start, j.actual_end))
                                               for j in g.jobs),
        "installations_actual_end_month_differs": sum(j.actual_end is not None and j.actual_end.astimezone(UTC).month
                                                      != local_date(j.actual_end).month for j in g.jobs),
        "accounts_created_date_differs": sum(utc_shifted(a.created) for a in g.accounts),
        "shifted_timestamps_on_dst_days": {d.isoformat(): sum(local_date(t) == d and utc_shifted(t) for t in stamps)
                                           for d in DST_DAYS},
        # Records whose local date (or month) a model with a wrong summer-time rule would get wrong
        "timestamps_wrong_with_rule": {name: sum(rule_local_date(t, rule) != local_date(t) for t in stamps)
                                       for name, rule in WRONG_RULES.items()},
        "orders_and_job_ends_moving_month_with_rule": {name: month_moves(g, WRONG_RULES[name])
                                                       for name in MONTH_MOVE_RULES},
    }


def build_expected(g: Generator, calc: Calc, seed: int, tables: dict) -> dict:
    months = []
    m = month_start(DATE_TABLE[0])
    while m <= DATE_TABLE[1]:
        months.append(m)
        m = next_month(m)
    month_ctx = lambda m, **kw: Ctx(dates=(m, month_end(m)), **kw)
    year_ctx = lambda y, **kw: Ctx(dates=(date(y, 1, 1), date(y, 12, 31)), **kw)
    owners = [u.name for u in g.users] + [NO_OWNER]
    # Owners of orders; the test checks that the other owners (coordinators, back office) have no values
    order_owners = SALES + [FORMER, NO_OWNER]
    groups = sorted({p.group for p in g.products}) + [WRITE_IN_GROUP]
    segments = list(SEGMENT_SHARE) + [NOT_SET]
    crews = CREWS + [NOT_ASSIGNED]
    by_number = {f.number: f for f in calc.facts}
    jobs_by_name = sorted(g.jobs, key=lambda j: j.name)
    year_measures = [m for m in SALES_MEASURES + OPS_MEASURES + DQ_MEASURES if m != "Jobs"]
    calendar = [DATE_TABLE[0] + days(n) for n in range((DATE_TABLE[1] - DATE_TABLE[0]).days + 1)]
    iso = lambda d: d.isoformat() if d is not None else None

    return {
        "meta": {
            "generated_by": "tools/generate_data.py",
            "seed": seed,
            "exported_on_utc": utc_text(EXPORTED_ON),
            "report_date": REPORT_DATE.isoformat(),
            "time_zone": "Europe/Warsaw (Python zoneinfo, independent of the M function)",
            "values": ("Keys are DAX measure names. Counts and sums: 0 means no rows, DAX may return BLANK or 0. "
                       "null means DIVIDE by zero or no rows for an average, DAX returns BLANK. Money is rounded "
                       "to 2 decimals (sums are exact), rates and averages are full precision."),
            # Read by tools/ModelTool (Tester.cs): money to the cent, rates and other decimals near-exact
            "tolerance": {"money": 0.005, "rate": 1e-6, "decimal": 1e-6, "count": 0},
            "money_measures": MONEY_MEASURES,
            "rate_measures": RATE_MEASURES,
            # The filter column(s) of each section; with two columns a key is "<value 1>|<value 2>"
            "filters": {
                "by_year": "'Date'[Year]",
                "by_month": "'Date'[Month Start]",
                "by_owner": "'Owner'[Owner]",
                "by_product_group": "'Product'[Product Group]",
                "by_segment": "'Customer'[Segment]",
                "by_installation_status": "'Orders'[Installation Status]",
                "by_crew": "'Crew'[Crew]",
                "by_year_owner": ["'Date'[Year]", "'Owner'[Owner]"],
                "by_month_owner": ["'Date'[Month Start]", "'Owner'[Owner]"],
                "by_month_segment": ["'Date'[Month Start]", "'Customer'[Segment]"],
                "dq_by_field": "'DQ Fields'[Field]",
                "dq_by_department": "'DQ Fields'[Responsible Department]",
                "dq_by_month": "'Date'[Month Start]",
                "dq_by_owner": "'Owner'[Owner]",
            },
            "semantics": [
                "Signed Orders / Signed Value count only orders with a Signed Date, also without a Date filter.",
                "Units Planned counts only orders with a Planned Install Month, also without a Date filter.",
                "Units Installed: jobs by local Actual End Date <= Report Date; the Date filter on Orders is removed.",
                "Order Value PY without filters = Order Value of 2025 (the year before the whole Date table).",
                "Order Value YTD window: 1 Jan of the year of the last date in context .. min(that date, Report Date); "
                "PY YTD: the same window one year earlier. Without filters: 2026-01-01..2026-10-28 vs "
                "2025-01-01..2025-10-28.",
                "Jobs and Units Installed by crew: Installations[Crew], '(not assigned)' when empty.",
                "Jobs counts non-cancelled jobs, also those of cancelled orders; Units Installed does not.",
                "Installation status: a job has started when it has an actual start or an actual end.",
            ],
        },
        "row_counts": {name: len(rows) for name, (_, rows) in tables.items()},
        "totals": calc.block(ALL_MEASURES, Ctx()),
        "by_year": {str(y): calc.block(year_measures, year_ctx(y)) for y in (2025, 2026)},
        "by_month": {m.isoformat(): calc.block(MONTH_MEASURES, month_ctx(m)) for m in months},
        "by_owner": {o: calc.block(OWNER_MEASURES, Ctx(owner=o)) for o in owners},
        "by_product_group": {gr: calc.block(["Order Value", "Units Ordered"], Ctx(group=gr)) for gr in groups},
        "by_segment": {s: calc.block(["Orders", "Order Value", "Units Installed"], Ctx(segment=s)) for s in segments},
        "by_installation_status": {s: calc.block(["Orders", "Cancelled Orders", "Units Installed"], Ctx(status=s))
                                   for s in INSTALLATION_STATUSES},
        "by_crew": {c: calc.block(["Jobs", "Units Installed"], Ctx(crew=c)) for c in crews},
        "by_year_owner": {f"{y}|{o}": calc.block(YEAR_OWNER_MEASURES, year_ctx(y, owner=o))
                          for y in (2025, 2026) for o in order_owners},
        "by_month_owner": {f"{m.isoformat()}|{o}": calc.block(MONTH_OWNER_MEASURES, month_ctx(m, owner=o))
                           for m in months for o in order_owners},
        "by_month_segment": {f"{m.isoformat()}|{s}": calc.block(MONTH_SEGMENT_MEASURES, month_ctx(m, segment=s))
                             for m in months for s in segments},
        "dq_by_field": {name: {"Field Key": key, "Record Type": rec, "Responsible Department": dept,
                               **calc.block(DQ_MEASURES, Ctx(dq_field=name))}
                        for key, name, rec, dept in DQ_FIELDS},
        "dq_by_department": {d: calc.block(DQ_MEASURES + ["Fields Below Target"], Ctx(dept=d)) for d in DEPARTMENTS},
        "dq_by_month": {m.isoformat(): calc.block(DQ_MEASURES, month_ctx(m)) for m in months},
        "dq_by_owner": {o: calc.block(DQ_MEASURES, Ctx(owner=o)) for o in owners},
        "utc_check": utc_checks(g, calc),
        "delayed_orders": {
            "columns": ["Order No", "Customer", "Owner", "Earliest Planned Start"],
            "rows": [[f.number, f.customer, f.owner, f.earliest_planned_start.isoformat()]
                     for f in sorted(calc.facts, key=lambda f: f.number) if f.status == "Delayed start"]},
        "rows": {
            # Dimension attributes that the report filters and labels by
            "dates": {
                "key": "'Date'[Date]",
                "columns": ["Date", "Year", "Quarter", "Month Number", "Month", "Month Start", "Is Past"],
                "rows": [[d.isoformat(), d.year, f"Q{(d.month - 1) // 3 + 1} {d.year}", d.month,
                          f"{MONTH_ABBR[d.month - 1]} {d.year}", month_start(d).isoformat(), d <= REPORT_DATE]
                         for d in calendar]},
            "owners": {
                "key": "'Owner'[Owner]",
                "columns": ["Owner", "Department", "Is Active"],
                "rows": [[u.name, u.department, not u.disabled] for u in g.users] + [[NO_OWNER, "(none)", False]]},
            "customers": {
                "key": "'Customer'[Customer]",
                "columns": ["Customer", "City", "Segment"],
                "rows": [[a.name, a.city or NOT_SET, a.segment or NOT_SET]
                         for a in sorted(g.accounts, key=lambda a: a.name)]},
            "products": {
                "key": "'Product'[Product No]",
                "columns": ["Product No", "Product", "Product Group", "Is Installable", "List Price"],
                "rows": [[p.number, p.name, p.group, p.installable, float(p.price)] for p in g.products]
                        + [["(write-in)", "(write-in)", WRITE_IN_GROUP, False, None]]},
            "orders": {
                "key": "'Orders'[Order No]",
                "columns": ["Order No", "Created Date", "Signed Date", "CRM Status", "Installable Units",
                            "Installed Units", "Installation Status", "Installed Date"],
                "rows": [[f.number, f.created.isoformat(), iso(f.signed), f.crm_status, f.installable_units,
                          f.installed_units, f.status, iso(f.installed_date)]
                         for f in (by_number[n] for n in sorted(by_number))]},
            "installations": {
                "key": "'Installations'[Installation No]",
                "columns": ["Installation No", "Crew", "Coordinator", "Actual Start Date", "Actual End Date",
                            "Job Status", "Created Date"],
                "rows": [[j.name, j.crew or NOT_ASSIGNED, j.coordinator.name if j.coordinator else NOT_ASSIGNED,
                          local_date(j.actual_start).isoformat() if j.actual_start else None,
                          local_date(j.actual_end).isoformat() if j.actual_end else None,
                          j.status, local_date(j.created).isoformat()] for j in jobs_by_name]},
        },
    }


def dumps(obj, level: int = 0) -> str:
    """JSON with nested objects indented and every list of scalars (a table row) on one line."""
    pad = "  " * level
    if isinstance(obj, dict) and obj:
        items = [f"{pad}  {json.dumps(k, ensure_ascii=False)}: {dumps(v, level + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(obj, list) and any(isinstance(x, (dict, list)) for x in obj):
        return "[\n" + ",\n".join(pad + "  " + dumps(x, level + 1) for x in obj) + "\n" + pad + "]"
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------- data-quality shape checks

def check_dq_shape(calc: Calc, expected: dict) -> None:
    for name, v in expected["dq_by_field"].items():
        c = v["Completeness %"]
        check(0.80 <= c <= 0.99, f"DQ {name}: completeness {c:.3f} outside 80-99%")
        check(abs(c - TARGET) >= 0.005, f"DQ {name}: completeness {c:.4f} too close to the target")
    planned = {o: calc.dq(Ctx(owner=o, dq_field="Planned install month")) for o in SALES}
    rate = {o: f / n for o, (n, f) in planned.items()}
    others = [r for o, r in rate.items() if o != "Owen Price"]
    check(rate["Owen Price"] < min(others) - 0.15, f"Planned install month by owner not uneven enough: {rate}")
    coord = {y: calc.dq(Ctx(dq_field="Coordinator", dates=(date(y, 1, 1), date(y, 12, 31)))) for y in (2025, 2026)}
    check(coord[2025][1] / coord[2025][0] < coord[2026][1] / coord[2026][0] - 0.08,
          "coordinator gaps are not concentrated in 2025")
    early = calc.dq(Ctx(dq_field="Specification date", dates=(date(2025, 1, 1), date(2025, 12, 31))))
    late = calc.dq(Ctx(dq_field="Specification date", dates=(date(2026, 7, 1), date(2026, 10, 31))))
    check(late[1] / late[0] < early[1] / early[0] - 0.10, "specification date gaps do not grow in recent months")
    statuses = expected["by_installation_status"]
    for s in INSTALLATION_STATUSES:
        n = statuses[s]["Orders"] or statuses[s]["Cancelled Orders"]
        check(n >= 5, f"status {s} has only {n} orders")
    check(expected["utc_check"]["orders_move_month_if_utc_dates_used"] > 0, "no order moves month with UTC dates")


def check_status_edges(g: Generator, calc: Calc) -> None:
    """Status cases that only the right job rules get right must be present with the expected status."""
    status = {f.number: f.status for f in calc.facts}
    installed_date = {f.number: f.installed_date for f in calc.facts}
    live = lambda o: [j for j in o.jobs if j.status != "Cancelled"]
    end_only = [o for o in g.orders if not o.cancelled and status[o.number] == "In progress"
                and not any(j.actual_start for j in live(o)) and any(j.actual_end for j in live(o))]
    check(len(end_only) >= 1, "no order in progress whose only started job has an end but no start")
    started_cancelled = [o for o in g.orders if not o.cancelled
                         and any(j.status == "Cancelled" and j.actual_start for j in o.jobs)
                         and not any(j.actual_start or j.actual_end for j in live(o))]
    check(started_cancelled and all(status[o.number] == "Scheduled" for o in started_cancelled),
          "orders with only a cancelled job started must be Scheduled")
    later = [o for o in g.orders if status[o.number] == "Installed"
             and any(j.status == "Cancelled" and j.actual_end and local_date(j.actual_end) > installed_date[o.number]
                     for j in o.jobs)]
    check(len(later) >= 2, "fewer than 2 installed orders with a cancelled job that ended later")


def check_additivity(expected: dict) -> None:
    """The breakdowns of additive measures must add up to the totals (every record lands in one bucket)."""
    t = expected["totals"]
    blocks = {
        "by_year": ["Orders", "Cancelled Orders", "Order Value", "Units Ordered", "Signed Orders", "Signed Value",
                    "Unsigned Orders", "Units Planned", "Units Installed", "Open Orders", "Delayed Starts",
                    "Orders Installed", "Units Backlog", "Orders Without Owner", "Checks", "Filled Values"],
        "by_month": ["Orders", "Cancelled Orders", "Order Value", "Units Ordered", "Signed Orders", "Signed Value",
                     "Units Planned", "Units Installed"],
        "by_owner": ["Orders", "Cancelled Orders", "Order Value", "Signed Value", "Unsigned Orders", "Open Orders",
                     "Delayed Starts", "Checks", "Filled Values", "Missing Values"],
        "by_product_group": ["Order Value", "Units Ordered"],
        "by_segment": ["Orders", "Order Value"],
        "by_installation_status": ["Orders", "Cancelled Orders"],
        "by_crew": ["Jobs", "Units Installed"],
        "dq_by_field": ["Checks", "Filled Values", "Missing Values"],
        "dq_by_department": ["Checks", "Filled Values", "Missing Values", "Fields Below Target"],
        "dq_by_month": ["Checks", "Filled Values", "Missing Values"],
        "dq_by_owner": ["Checks", "Filled Values", "Missing Values"],
    }
    for block, names in blocks.items():
        for m in names:
            total = sum(Decimal(str(v[m])) for v in expected[block].values())
            check(total == Decimal(str(t[m])), f"{block}: {m} adds up to {total}, total is {t[m]}")
    months = expected["by_month"]
    for key, v in months.items():
        if key.startswith("2026"):
            check(v["Order Value PY"] == months["2025" + key[4:]]["Order Value"], f"{key}: Order Value PY")
    check(t["Order Value PY"] == expected["by_year"]["2025"]["Order Value"], "total Order Value PY")
    check(t["Order Value YTD"] == expected["by_year"]["2026"]["Order Value YTD"], "total Order Value YTD")
    check(t["Orders Installed"] == expected["by_installation_status"]["Installed"]["Orders"], "Orders Installed")
    check(t["Delayed Starts"] == len(expected["delayed_orders"]["rows"]), "delayed orders list")
    # Two-filter blocks: summed over the second filter they give the one-filter block
    for block, single, names in (("by_year_owner", "by_year", ["Orders", "Order Value", "Signed Value", "Units Installed"]),
                                 ("by_month_owner", "by_month", ["Orders", "Order Value", "Signed Value",
                                                                 "Units Planned", "Units Installed"]),
                                 ("by_month_segment", "by_month", ["Orders", "Order Value", "Units Ordered",
                                                                   "Units Installed"])):
        for key, v in expected[single].items():
            for m in names:
                parts = [Decimal(str(x[m])) for k, x in expected[block].items() if k.split("|")[0] == key]
                check(sum(parts) == Decimal(str(v[m])), f"{block}: {m} of {key} adds up to {sum(parts)}, not {v[m]}")


def check_text_coverage(expected: dict) -> None:
    """The compared text must include Polish letters and the CSV quoting cases, or a wrong encoding or
    quote setting in Csv.Document would pass unnoticed."""
    cells = [c for block in expected["rows"].values() for row in block["rows"] for c in row if isinstance(c, str)]
    cells += [c for row in expected["delayed_orders"]["rows"] for c in row if isinstance(c, str)]
    check(any(any(ord(ch) > 127 for ch in c) for c in cells), "no compared text with non-ASCII letters")
    check(any('"' in c for c in cells), "no compared text with a double quote")
    check(any("," in c for c in cells), "no compared text with a comma")
    cities = {r[1] for r in expected["rows"]["customers"]["rows"]}
    check(any(any(ord(ch) > 127 for ch in c) for c in cities), "no city with Polish letters is compared")


# ---------------------------------------------------------------- main

def edge_summary(g: Generator) -> dict:
    tags = Counter(j.tag.split(" 20")[0] for j in g.jobs if j.tag) + Counter(o.tag for o in g.orders if o.tag)
    lines = [l for o in g.orders for l in o.lines]
    return {
        "write-in lines": sum(l.product is None for l in lines),
        "lines with a manual discount": sum(l.discount > 0 for l in lines),
        "orders without owner": sum(o.owner is None for o in g.orders),
        "orders owned by the disabled user": sum(bool(o.owner and o.owner.disabled) for o in g.orders),
        "accounts owned by the disabled user": sum(a.owner.disabled for a in g.accounts),
        "accounts without orders": len({a.id for a in g.accounts} - {o.account.id for o in g.orders}),
        "accounts without city": sum(a.city is None for a in g.accounts),
        "accounts without segment": sum(a.segment is None for a in g.accounts),
        "account names needing CSV quotes": sum("," in a.name or '"' in a.name for a in g.accounts),
        "orders with nothing to install": sum(not o.jobs for o in g.orders),
        "split installations (2 live jobs)": sum(len([j for j in o.jobs if j.status != "Cancelled"]) == 2
                                                 for o in g.orders),
        "completed jobs without actual start": sum(j.status == "Completed" and j.actual_start is None for j in g.jobs),
        "live jobs of cancelled orders": sum(o.cancelled and j.status != "Cancelled" for o in g.orders for j in o.jobs),
        "cancelled jobs with an actual start": sum(j.status == "Cancelled" and j.actual_start is not None
                                                   for j in g.jobs),
        "in-progress jobs with units entered": sum(j.status == "In progress" and j.installed_units > 0
                                                   for j in g.jobs),
        "jobs by status": dict(Counter(j.status for j in g.jobs)),
        "edge records by tag": dict(sorted(tags.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic Dataverse export and expected figures.")
    parser.add_argument("--seed", type=int, default=2610, help="random seed (default 2610)")
    args = parser.parse_args()

    g = Generator(args.seed)
    check_truth(g)
    tables = table_rows(g)
    write_csvs(tables)
    read_back(tables)
    calc = Calc(g)
    expected = build_expected(g, calc, args.seed, tables)
    check_dq_shape(calc, expected)
    check_status_edges(g, calc)
    check_additivity(expected)
    check_text_coverage(expected)
    (DATA / "expected.json").write_text(dumps(expected) + "\n", encoding="utf-8", newline="\n")

    t = expected["totals"]
    print(f"Seed {args.seed}, Report Date {REPORT_DATE}, written to {DATA}")
    for name, n in expected["row_counts"].items():
        print(f"  {name:<24} {n:>6} rows")
    print("Totals:")
    for m in ALL_MEASURES:
        print(f"  {m:<30} {t[m]}")
    print("2025 vs 2026:")
    for m in ["Orders", "Order Value", "Order Value YTD", "Order Value PY YTD", "YTD Growth %", "Units Installed"]:
        print(f"  {m:<30} {expected['by_year']['2025'][m]!s:>16} {expected['by_year']['2026'][m]!s:>16}")
    print("Installation status:", {s: v["Orders"] or v["Cancelled Orders"]
                                   for s, v in expected["by_installation_status"].items()})
    print("DQ by field:")
    for name, v in expected["dq_by_field"].items():
        print(f"  {name:<24} {v['Checks']:>6} checks {v['Missing Values']:>5} missing  {v['Completeness %']:.1%}")
    print("UTC check:", json.dumps(expected["utc_check"], ensure_ascii=False))
    print("Edge cases:", json.dumps(edge_summary(g), ensure_ascii=False))
    print("All self-checks passed.")


if __name__ == "__main__":
    main()
