#!/usr/bin/env python3
"""Write the PBIR report of SalesOps.Report: report.json, the three pages, every visual and the custom theme.

This script is the source of the report. It rewrites definition/report.json, definition/pages/ and
StaticResources/RegisteredResources/ from scratch; definition.pbir, definition/version.json and the base
theme (StaticResources/SharedResources) are kept as Power BI Desktop wrote them. Change the report here
and run the script again, rather than editing the JSON by hand: a hand edit is lost on the next run.
If the report is changed and saved in Power BI Desktop instead, stop using the script.

Usage:  python -I tools/build_report.py [--active sales|operations|dataquality]
"""
import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "SalesOps.Report"
DEF = REPORT / "definition"

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
VERSIONS = {"visual": "2.13.0", "report": "3.3.0", "page": "2.1.0"}
THEME_FILE = "SalesOpsTheme.json"

TEAL = "#16524C"
TEAL_MID = "#4F8A83"
TEAL_LIGHT = "#A8C8C4"
WARM = "#D9822B"
GREY_DARK = "#3B4445"
GREY = "#6B7778"
GREY_LIGHT = "#D7DEDD"
PAGE_BG = "#EEF2F1"

M = "_Measures"


# ---------- expression helpers ----------
def lit(value):
    return {"expr": {"Literal": {"Value": value}}}


def text(s):
    return lit("'" + s.replace("'", "''") + "'")


def color(hex_):
    return {"solid": {"color": text(hex_)}}


def col(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def meas(prop):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": M}}, "Property": prop}}


def proj(field, active=None, display=None):
    kind = "Column" if "Column" in field else "Measure"
    entity = field[kind]["Expression"]["SourceRef"]["Entity"]
    prop = field[kind]["Property"]
    p = {"field": field, "queryRef": f"{entity}.{prop}", "nativeQueryRef": prop}
    if display:
        p["displayName"] = display
    if active is not None:
        p["active"] = active
    return p


def role(*projections):
    return {"projections": list(projections)}


def sort(field, direction="Ascending"):
    return {"sort": [{"field": field, "direction": direction}], "isDefaultSort": False}


def title(s):
    return {"title": [{"properties": {"show": lit("true"), "text": text(s)}}]}


def no_title():
    return {"title": [{"properties": {"show": lit("false")}}]}


def categorical_filter(name, entity, prop, values):
    """Visual-level 'In' filter on a column (values are DAX-ready literals: 'text' or true/false)."""
    return {
        "name": name,
        "field": col(entity, prop),
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": entity, "Type": 0}],
            "Where": [{
                "Condition": {
                    "In": {
                        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}}],
                        "Values": [[{"Literal": {"Value": v}}] for v in values],
                    }
                }
            }],
        },
        "howCreated": "User",
    }


def not_blank_filter(name, entity, prop):
    """Visual-level 'is not blank' filter on a column."""
    column = {"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}}
    return {
        "name": name,
        "field": col(entity, prop),
        "type": "Advanced",
        "filter": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": entity, "Type": 0}],
            "Where": [{"Condition": {"Not": {"Expression": {"Comparison": {
                "ComparisonKind": 0, "Left": column, "Right": {"Literal": {"Value": "null"}}}}}}}],
        },
        "howCreated": "User",
    }


# ---------- visuals ----------
class Page:
    def __init__(self, name, display):
        self.name = name
        self.display = display
        self.visuals = []

    def add(self, name, x, y, w, h, visual, filters=None):
        v = {
            "$schema": f"{SCHEMA}/visualContainer/2.13.0/schema.json",
            "name": name,
            "position": {"x": x, "y": y, "z": len(self.visuals) * 1000, "height": h, "width": w,
                         "tabOrder": len(self.visuals) * 1000},
            "visual": visual,
        }
        if filters:
            v["filterConfig"] = {"filters": filters}
        self.visuals.append(v)


def textbox(heading, subtitle):
    return {
        "visualType": "textbox",
        "objects": {
            "general": [{
                "properties": {
                    "paragraphs": [
                        {"textRuns": [{"value": heading, "textStyle": {
                            "fontFamily": "Segoe UI Semibold", "fontSize": "18pt", "color": TEAL}}]},
                        {"textRuns": [{"value": subtitle, "textStyle": {
                            "fontFamily": "Segoe UI", "fontSize": "9pt", "color": GREY}}]},
                    ]
                }
            }]
        },
        "drillFilterOtherVisuals": True,
    }


def card(measure, font=22, units="1D", label=10):
    return {
        "visualType": "card",
        "query": {"queryState": {"Values": role(proj(meas(measure)))}},
        "objects": {
            "labels": [{"properties": {
                "fontSize": lit(f"{font}D"),
                "labelDisplayUnits": lit(units),
                "color": color(TEAL),
            }}],
            "categoryLabels": [{"properties": {"fontSize": lit(f"{label}D"), "color": color(GREY)}}],
        },
        "visualContainerObjects": no_title(),
        "drillFilterOtherVisuals": True,
    }


def slicer(entity, prop, header):
    return {
        "visualType": "slicer",
        "query": {"queryState": {"Values": role(proj(col(entity, prop), active=True))},
                  "sortDefinition": sort(col(entity, prop))},
        "objects": {
            # Tiles show every value; a dropdown shows an "All" placeholder in the Desktop UI language
            "data": [{"properties": {"mode": text("Basic")}}],
            "general": [{"properties": {"orientation": lit("1D"), "responsive": lit("true")}}],
            "header": [{"properties": {"textSize": lit("9D")}}],
            "items": [{"properties": {"textSize": lit("9D")}}],
        },
        "drillFilterOtherVisuals": True,
    }


def axis_objects(legend=True):
    objs = {
        "categoryAxis": [{"properties": {"showAxisTitle": lit("false")}}],
        "valueAxis": [{"properties": {"showAxisTitle": lit("false"), "labelDisplayUnits": lit("1D")}}],
    }
    if legend:
        objs["legend"] = [{"properties": {"show": lit("true"), "position": text("Top")}}]
    else:
        objs["legend"] = [{"properties": {"show": lit("false")}}]
    return objs


def series_colors(objs, pairs):
    """pairs: [(measure, hex)] - fixes the colour of each measure series."""
    objs["dataPoint"] = [
        {"properties": {"fill": color(c)}, "selector": {"metadata": f"{M}.{m}"}} for m, c in pairs
    ]
    return objs


def table(fields, chart_title, sort_field=None, direction="Ascending", totals=True):
    q = {"queryState": {"Values": role(*fields)}}
    if sort_field is not None:
        q["sortDefinition"] = sort(sort_field, direction)
    objs = {
        # The default total label comes from the Desktop UI language
        "total": [{"properties": {"totals": lit("true" if totals else "false"), "label": text("Total")}}],
        "columnHeaders": [{"properties": {"columnAdjustment": text("growToFit")}}],
    }
    return {
        "visualType": "tableEx",
        "query": q,
        "objects": objs,
        "visualContainerObjects": title(chart_title),
        "drillFilterOtherVisuals": True,
    }


# ---------- pages ----------
MARGIN, GAP, W = 16, 16, 1280
INNER = W - 2 * MARGIN
HEAD_Y, HEAD_H = 8, 64
KPI_Y, KPI_H = 84, 76
ROW2_Y = KPI_Y + KPI_H + GAP
ROW2_H = 252


def kpi_row(page, names, measures):
    n = len(measures)
    w = (INNER - GAP * (n - 1)) / n
    for i, (name, m) in enumerate(zip(names, measures)):
        page.add(name, round(MARGIN + i * (w + GAP), 2), KPI_Y, round(w, 2), KPI_H, card(m))


def header(page, heading, subtitle, title_w):
    page.add("title", MARGIN, HEAD_Y, title_w, HEAD_H, textbox(heading, subtitle))
    page.add("reportDate", W - MARGIN - 128, HEAD_Y, 128, HEAD_H, card("Report Date", font=14, label=9))


def sales_page():
    p = Page("sales", "Sales")
    header(p, "Sales", "Created date, Warsaw time · EUR · excl. cancelled", 320)
    # The Date table has a blank member (unsigned orders have no signed date), so the slicer hides blanks
    p.add("slicerYear", 352, HEAD_Y, 192, HEAD_H, slicer("Date", "Year", "Year"),
          filters=[not_blank_filter("yearNotBlank", "Date", "Year")])
    p.add("slicerOwner", 560, HEAD_Y, 560, HEAD_H, slicer("Owner", "Owner", "Owner"),
          filters=[categorical_filter("ownerSales", "Owner", "Department", ["'Sales'"])])
    kpi_row(p, ["cardOrderValue", "cardOrders", "cardAvgOrder", "cardYtdGrowth"],
            ["Order Value", "Orders", "Average Order Value", "YTD Growth %"])

    month = col("Date", "Month")
    objs = axis_objects()
    # secShow false puts the line on the same scale as the columns
    objs["valueAxis"] = [{"properties": {"showAxisTitle": lit("false"), "labelDisplayUnits": lit("1D"),
                                         "secShow": lit("false")}}]
    series_colors(objs, [("Order Value", TEAL), ("Order Value PY", WARM)])
    p.add("chartValueByMonth", MARGIN, ROW2_Y, INNER, ROW2_H, {
        "visualType": "lineClusteredColumnComboChart",
        "query": {"queryState": {
            "Category": role(proj(month, active=True)),
            "Y": role(proj(meas("Order Value"))),
            "Y2": role(proj(meas("Order Value PY"))),
        }, "sortDefinition": sort(month)},
        "objects": objs,
        "visualContainerObjects": title("Order value by month, with the same month a year earlier"),
        "drillFilterOtherVisuals": True,
    })

    row3_y = ROW2_Y + ROW2_H + GAP
    row3_h = 720 - MARGIN - row3_y
    group = col("Product", "Product Group")
    objs = axis_objects(legend=False)
    objs["labels"] = [{"properties": {"show": lit("true"), "labelDisplayUnits": lit("1D"), "labelPrecision": lit("0L")}}]
    objs["valueAxis"] = [{"properties": {"show": lit("false"), "showAxisTitle": lit("false")}}]
    series_colors(objs, [("Order Value", TEAL)])
    p.add("chartValueByGroup", MARGIN, row3_y, 488, row3_h, {
        "visualType": "clusteredBarChart",
        "query": {"queryState": {
            "Category": role(proj(group, active=True)),
            "Y": role(proj(meas("Order Value"))),
        }, "sortDefinition": sort(meas("Order Value"), "Descending")},
        "objects": objs,
        "visualContainerObjects": title("Order value by product group"),
        "drillFilterOtherVisuals": True,
    })
    p.add("tableOwners", MARGIN + 488 + GAP, row3_y, INNER - 488 - GAP, row3_h, table(
        [proj(col("Owner", "Owner")), proj(meas("Orders")), proj(meas("Order Value")),
         proj(meas("Signed Value")), proj(meas("Unsigned Orders"))],
        "Sales by owner", meas("Order Value"), "Descending"))
    return p


def operations_page():
    p = Page("operations", "Operations")
    header(p, "Operations", "Installation status as of the report date. Units are installable units "
           "(services and write-in lines excluded).", 1104)
    kpi_row(p, ["cardUnitsPlanned", "cardUnitsInstalled", "cardInstallRate", "cardOpenOrders", "cardDelayed"],
            ["Units Planned", "Units Installed", "Installation Rate %", "Open Orders", "Delayed Starts"])

    month = col("Date", "Month")
    objs = axis_objects()
    series_colors(objs, [("Units Planned", TEAL_LIGHT), ("Units Installed", TEAL)])
    p.add("chartUnitsByMonth", MARGIN, ROW2_Y, 816, ROW2_H, {
        "visualType": "clusteredColumnChart",
        "query": {"queryState": {
            "Category": role(proj(month, active=True)),
            "Y": role(proj(meas("Units Planned")), proj(meas("Units Installed"))),
        }, "sortDefinition": sort(month)},
        "objects": objs,
        "visualContainerObjects": title("Units planned (by planned month) vs installed (by end date)"),
        "drillFilterOtherVisuals": True,
    })

    status = col("Orders", "Installation Status")
    objs = axis_objects(legend=False)
    objs["labels"] = [{"properties": {"show": lit("true")}}]
    objs["dataPoint"] = [
        {"properties": {"fill": color(TEAL)}},
        {"properties": {"fill": color(WARM)}, "selector": {"data": [{"scopeId": {"Comparison": {
            "ComparisonKind": 0,
            "Left": status,
            "Right": {"Literal": {"Value": "'Delayed start'"}},
        }}}]}},
    ]
    p.add("chartStatus", MARGIN + 816 + GAP, ROW2_Y, INNER - 816 - GAP, ROW2_H, {
        "visualType": "clusteredBarChart",
        "query": {"queryState": {
            "Category": role(proj(status, active=True)),
            "Y": role(proj(meas("Orders"))),
        }, "sortDefinition": sort(status)},
        "objects": objs,
        "visualContainerObjects": title("Orders by installation status"),
        "drillFilterOtherVisuals": True,
    })

    row3_y = ROW2_Y + ROW2_H + GAP
    row3_h = 720 - MARGIN - row3_y
    p.add("tableCrews", MARGIN, row3_y, 400, row3_h, table(
        [proj(col("Crew", "Crew")), proj(meas("Jobs")), proj(meas("Units Installed"))],
        "Installation jobs by crew", col("Crew", "Crew")))
    p.add("tableDelayed", MARGIN + 400 + GAP, row3_y, INNER - 400 - GAP, row3_h, table(
        [proj(col("Orders", "Order No")), proj(col("Customer", "Customer")), proj(col("Owner", "Owner")),
         proj(col("Orders", "First Planned Start"), display="Earliest Planned Start")],
        "Delayed starts: earliest planned start before the report date, no job started",
        col("Orders", "First Planned Start"), totals=False),
        filters=[categorical_filter("delayedOnly", "Orders", "Installation Status", ["'Delayed start'"])])
    return p


def quality_page():
    p = Page("dataquality", "Data Quality")
    header(p, "Data Quality", "Share of CRM records where a required field is filled. "
           "Target 95%. Checks are dated by the record's created date.", 1104)
    kpi_row(p, ["cardCompleteness", "cardMissing", "cardBelowTarget"],
            ["Completeness %", "Missing Values", "Fields Below Target"])

    # Row 2: the nine fields as bars and as a table; row 3: the trend and the owners
    row2_h = 280
    row3_y = ROW2_Y + row2_h + GAP
    row3_h = 720 - MARGIN - row3_y
    field = col("DQ Fields", "Field")
    objs = axis_objects(legend=False)
    objs["labels"] = [{"properties": {"show": lit("true")}}]
    objs["valueAxis"] = [{"properties": {"show": lit("false"), "showAxisTitle": lit("false")}}]
    # Bars below the 95% target in the warm accent
    objs["dataPoint"] = [{
        "properties": {"fill": {"solid": {"color": {"expr": {"Conditional": {
            "Cases": [{
                "Condition": {"Comparison": {"ComparisonKind": 3, "Left": meas("Completeness %"),
                                             "Right": {"Literal": {"Value": "0.95D"}}}},
                "Value": {"Literal": {"Value": f"'{WARM}'"}},
            }],
            "DefaultValue": {"Literal": {"Value": f"'{TEAL}'"}},
        }}}}}},
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]},
    }]
    p.add("chartByField", MARGIN, ROW2_Y, 488, row2_h, {
        "visualType": "clusteredBarChart",
        "query": {"queryState": {
            "Category": role(proj(field, active=True)),
            "Y": role(proj(meas("Completeness %"))),
        }, "sortDefinition": sort(meas("Completeness %"), "Ascending")},
        "objects": objs,
        "visualContainerObjects": title("Completeness by field (below target in orange)"),
        "drillFilterOtherVisuals": True,
    })

    month = col("Date", "Month")
    objs = axis_objects()
    series_colors(objs, [("Completeness %", TEAL), ("Target Completeness", WARM)])
    p.add("chartByMonth", MARGIN, row3_y, 816, row3_h, {
        "visualType": "lineChart",
        "query": {"queryState": {
            "Category": role(proj(month, active=True)),
            "Y": role(proj(meas("Completeness %")), proj(meas("Target Completeness"))),
        }, "sortDefinition": sort(month)},
        "objects": objs,
        "visualContainerObjects": title("Completeness by month of record creation"),
        "drillFilterOtherVisuals": True,
    }, filters=[categorical_filter("pastOnly", "Date", "Is Past", ["true"])])

    p.add("tableFields", MARGIN + 488 + GAP, ROW2_Y, INNER - 488 - GAP, row2_h, table(
        [proj(field), proj(col("DQ Fields", "Record Type")), proj(col("DQ Fields", "Responsible Department")),
         proj(meas("Checks")), proj(meas("Missing Values")), proj(meas("Completeness %"))],
        "Fields, record types and responsible departments", field))
    p.add("tableOwners", MARGIN + 816 + GAP, row3_y, INNER - 816 - GAP, row3_h, table(
        [proj(col("Owner", "Owner")), proj(meas("Missing Values")), proj(meas("Completeness %"))],
        "Completeness by record owner", meas("Completeness %"), totals=False))
    return p


# ---------- theme ----------
def theme():
    return {
        "name": "SalesOps",
        "dataColors": [TEAL, WARM, TEAL_LIGHT, GREY, TEAL_MID, "#F2C38F", GREY_DARK, "#B8C2C1"],
        "foreground": GREY_DARK,
        "foregroundNeutralSecondary": GREY,
        "foregroundNeutralTertiary": "#A3ADAC",
        "background": "#FFFFFF",
        "backgroundLight": "#F5F7F7",
        "backgroundNeutral": GREY_LIGHT,
        "tableAccent": TEAL,
        "good": TEAL,
        "neutral": "#F2C38F",
        "bad": WARM,
        "maximum": TEAL,
        "center": TEAL_LIGHT,
        "minimum": "#EAF2F1",
        "textClasses": {
            "callout": {"fontSize": 22, "fontFace": "Segoe UI Semibold", "color": TEAL},
            "title": {"fontSize": 11, "fontFace": "Segoe UI Semibold", "color": GREY_DARK},
            "header": {"fontSize": 10, "fontFace": "Segoe UI Semibold", "color": GREY_DARK},
            "label": {"fontSize": 9, "fontFace": "Segoe UI", "color": GREY},
        },
        "visualStyles": {
            "*": {"*": {
                "background": [{"show": True, "color": {"solid": {"color": "#FFFFFF"}}, "transparency": 0}],
                "border": [{"show": True, "color": {"solid": {"color": GREY_LIGHT}}, "radius": 6}],
                "title": [{"show": True, "alignment": "left", "fontSize": 11, "fontColor": {"solid": {"color": GREY_DARK}}}],
            }},
            "page": {"*": {
                "background": [{"color": {"solid": {"color": PAGE_BG}}, "transparency": 0}],
                "outspace": [{"color": {"solid": {"color": PAGE_BG}}, "transparency": 0}],
            }},
            "textbox": {"*": {
                "background": [{"show": False}],
                "border": [{"show": False}],
            }},
            "tableEx": {"*": {
                "columnHeaders": [{"fontColor": {"solid": {"color": TEAL}}, "bold": True}],
                "total": [{"fontColor": {"solid": {"color": TEAL}}}],
            }},
        },
    }


# ---------- write ----------
def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    # LF line ends on every OS, like the TMDL files
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def main():
    pages = [sales_page(), operations_page(), quality_page()]
    parser = argparse.ArgumentParser(description="Write the PBIR report of SalesOps.Report.")
    parser.add_argument("--active", choices=[p.name for p in pages], default=pages[0].name,
                        help="page shown when the report opens (default: sales)")
    active = parser.parse_args().active

    shutil.rmtree(DEF / "pages", ignore_errors=True)
    shutil.rmtree(REPORT / "StaticResources" / "RegisteredResources", ignore_errors=True)

    dump(REPORT / "StaticResources" / "RegisteredResources" / THEME_FILE, theme())
    dump(DEF / "report.json", {
        "$schema": f"{SCHEMA}/report/3.3.0/schema.json",
        "themeCollection": {
            "baseTheme": {"name": "CY26SU10", "reportVersionAtImport": VERSIONS, "type": "SharedResources"},
            "customTheme": {"name": THEME_FILE, "reportVersionAtImport": VERSIONS, "type": "RegisteredResources"},
        },
        "resourcePackages": [
            {"name": "SharedResources", "type": "SharedResources",
             "items": [{"name": "CY26SU10", "path": "BaseThemes/CY26SU10.json", "type": "BaseTheme"}]},
            {"name": "RegisteredResources", "type": "RegisteredResources",
             "items": [{"name": THEME_FILE, "path": THEME_FILE, "type": "CustomTheme"}]},
        ],
        "settings": {"locale": "en-US", "useStylableVisualContainerHeader": True,
                     "defaultDrillFilterOtherVisuals": True},
        "objects": {"outspacePane": [{"properties": {"expanded": lit("false")}}]},
    })
    dump(DEF / "pages" / "pages.json", {
        "$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
        "pageOrder": [p.name for p in pages],
        "activePageName": active,
    })
    for p in pages:
        dump(DEF / "pages" / p.name / "page.json", {
            "$schema": f"{SCHEMA}/page/2.1.0/schema.json",
            "name": p.name, "displayName": p.display, "displayOption": "FitToPage",
            "height": 720, "width": 1280,
        })
        for v in p.visuals:
            dump(DEF / "pages" / p.name / "visuals" / v["name"] / "visual.json", v)
    print(f"wrote {sum(len(p.visuals) for p in pages)} visuals on {len(pages)} pages; active page {active}")


if __name__ == "__main__":
    main()
