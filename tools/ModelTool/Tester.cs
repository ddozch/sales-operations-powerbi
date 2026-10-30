using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.AnalysisServices.AdomdClient;

namespace ModelTool;

// Golden-master test. data/expected.json is computed in Python from the generator's truth records,
// independently of the M and DAX code. Every figure in it is compared with the result of a DAX query
// against the model in the running engine of Power BI Desktop.
sealed partial class Tester
{
    const int FailLinesPerSection = 25;
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    // CSV file of the export -> rows of the model table loaded from it. The "(no owner)" and
    // "(write-in)" rows are added by the model, so they are not counted.
    static readonly Dictionary<string, string> RowCountQueries = new()
    {
        ["export_info.csv"] = "COUNTROWS ( Snapshot )",
        ["systemuser.csv"] = "COUNTROWS ( FILTER ( Owner, Owner[Owner Key] <> \"(no owner)\" ) )",
        ["account.csv"] = "COUNTROWS ( Customer )",
        ["product.csv"] = "COUNTROWS ( FILTER ( Product, Product[Product Key] <> \"(write-in)\" ) )",
        ["salesorder.csv"] = "COUNTROWS ( Orders )",
        ["salesorderdetail.csv"] = "COUNTROWS ( 'Order Lines' )",
        ["ops_installation.csv"] = "COUNTROWS ( Installations )",
    };

    // The delayed-orders table of the Operations page: the rows and the column behind each heading
    const string DelayedOrdersTable = "FILTER ( Orders, Orders[Installation Status] = \"Delayed start\" )";
    static readonly Dictionary<string, string> DelayedOrderColumns = new()
    {
        ["Order No"] = "Orders[Order No]",
        ["Customer"] = "RELATED ( Customer[Customer] )",
        ["Owner"] = "RELATED ( Owner[Owner] )",
        ["Earliest Planned Start"] = "Orders[First Planned Start]",
    };

    enum Kind { Count, Money, Rate, Decimal, Text }

    readonly AdomdConnection conn;
    readonly bool allFails, quiet;
    readonly HashSet<string> moneyMeasures, rateMeasures, modelMeasures;
    // Allowed absolute difference per kind of value, from meta.tolerance of expected.json
    readonly Dictionary<Kind, double> tolerance;
    readonly Dictionary<Kind, double> largestDiff = [];
    readonly List<(string Name, int Passed, int Failed)> results = [];

    // State of the section being run
    string section = "";
    int passed, failed, planned, shown;

    Tester(AdomdConnection conn, JsonElement meta, bool allFails, bool quiet)
    {
        this.conn = conn;
        this.allFails = allFails;
        this.quiet = quiet;
        moneyMeasures = Names(meta, "money_measures");
        rateMeasures = Names(meta, "rate_measures");
        var t = meta.GetProperty("tolerance");
        tolerance = new()
        {
            [Kind.Money] = t.GetProperty("money").GetDouble(),
            [Kind.Rate] = t.GetProperty("rate").GetDouble(),
            [Kind.Decimal] = t.GetProperty("decimal").GetDouble(),
            [Kind.Count] = t.GetProperty("count").GetDouble(),
        };
        modelMeasures = Query("SELECT [Name] FROM $SYSTEM.TMSCHEMA_MEASURES").Select(r => (string)r[0]!).ToHashSet();
    }

    static HashSet<string> Names(JsonElement meta, string property) =>
        meta.TryGetProperty(property, out var list) ? list.EnumerateArray().Select(e => e.GetString()!).ToHashSet() : [];

    public static int Run(string port, string expectedFile, bool allFails)
    {
        var watch = Stopwatch.StartNew();
        using var doc = JsonDocument.Parse(File.ReadAllBytes(expectedFile));
        var root = doc.RootElement;
        var meta = root.GetProperty("meta");
        using var conn = Engine.Open(port);
        var t = new Tester(conn, meta, allFails, quiet: false);

        Console.WriteLine($"Golden-master test: model on localhost:{port} against {Path.GetFullPath(expectedFile)}");
        Console.WriteLine($"Seed {meta.GetProperty("seed")}, report date {meta.GetProperty("report_date").GetString()}; tolerance " +
                          string.Join(", ", t.tolerance.Select(k => string.Create(Inv, $"{k.Key.ToString().ToLowerInvariant()} {k.Value}"))));
        t.RunSections(root, meta.GetProperty("filters"));

        var p = t.results.Sum(r => r.Passed);
        var f = t.results.Sum(r => r.Failed);
        Console.WriteLine();
        Console.WriteLine("Largest difference: " + string.Join(", ",
            t.largestDiff.OrderBy(d => d.Key).Select(d => string.Create(Inv, $"{d.Key.ToString().ToLowerInvariant()} {d.Value:G3}"))));
        Console.WriteLine(string.Create(Inv, $"TOTAL: {p} passed, {f} failed ({watch.Elapsed.TotalSeconds:0.0} s)"));
        return f == 0 ? 0 : 1;
    }

    // The whole test without output, for the mutation run: (passed, failed, failing sections with their counts)
    public static (int Passed, int Failed, List<(string Name, int Failed)> Sections) RunQuiet(AdomdConnection conn, JsonElement root)
    {
        var meta = root.GetProperty("meta");
        var t = new Tester(conn, meta, allFails: false, quiet: true);
        t.RunSections(root, meta.GetProperty("filters"));
        return (t.results.Sum(r => r.Passed), t.results.Sum(r => r.Failed),
                t.results.Where(r => r.Failed > 0).Select(r => (r.Name, r.Failed)).ToList());
    }

    void Write(string line)
    {
        if (!quiet) Console.WriteLine(line);
    }

    void RunSections(JsonElement root, JsonElement filters)
    {
        Write("");
        foreach (var s in root.EnumerateObject())
        {
            var value = s.Value;
            switch (s.Name)
            {
                case "meta":
                    break;
                case "utc_check":
                    Section(s.Name, () => UtcCheck(value));
                    break;
                case "row_counts":
                    Section(s.Name, () => RowCounts(value));
                    break;
                case "totals":
                    Section(s.Name, () => Totals(value));
                    break;
                case "delayed_orders":
                    Section(s.Name, () => Rows(value.GetProperty("columns"), value.GetProperty("rows"),
                        DelayedOrdersTable, c => DelayedOrderColumns.GetValueOrDefault(c)));
                    break;
                case "rows":
                    foreach (var r in value.EnumerateObject())
                    {
                        var block = r.Value;
                        var (table, _) = ParseColumn(block.GetProperty("key").GetString()!);
                        Section("rows." + r.Name, () => Rows(block.GetProperty("columns"), block.GetProperty("rows"),
                            QuoteTable(table), c => QuoteTable(table) + QuoteName(c)));
                    }
                    break;
                default:
                    // One filter column ("'Date'[Year]") or several (["'Date'[Year]", "'Owner'[Owner]"])
                    if (filters.TryGetProperty(s.Name, out var column))
                        Section(s.Name, () => Grouped(value, column.ValueKind == JsonValueKind.Array
                            ? column.EnumerateArray().Select(c => c.GetString()!).ToList()
                            : [column.GetString()!]));
                    else
                        Section(s.Name, () => Fail("no DAX mapping for this section of expected.json"));
                    break;
            }
        }
    }

    void Section(string name, Action body)
    {
        section = name;
        passed = failed = planned = shown = 0;
        try
        {
            body();
        }
        catch (AdomdException ex)
        {
            // A failing query fails every check it would have answered
            Fail("query error: " + ex.Message.Split('\n')[0].Trim(), Math.Max(1, planned - passed - failed));
        }
        if (shown < failed && !allFails) Write($"  ... {failed - shown} more failure(s) in {name} (use --all-fails)");
        results.Add((name, passed, failed));
        Write($"{name,-24} {passed,6} passed, {failed,4} failed");
    }

    void Pass() => passed++;

    void Fail(string message, int count = 1)
    {
        failed += count;
        if (allFails || shown < FailLinesPerSection)
        {
            Write($"  FAIL {section} {message}");
            shown += count;
        }
    }

    // ------------------------------------------------------------------ sections

    // Preconditions: the data must contain the time-zone edge cases, or a wrong conversion would pass
    void UtcCheck(JsonElement checks)
    {
        foreach (var (name, value) in Flatten(checks, ""))
        {
            if (value.ValueKind == JsonValueKind.Number && value.GetDouble() > 0) Pass();
            else Fail($"{name} = {value.GetRawText()}: the data has no such case, so a wrong time conversion could pass");
        }
    }

    static IEnumerable<(string, JsonElement)> Flatten(JsonElement e, string prefix) =>
        e.ValueKind == JsonValueKind.Object
            ? e.EnumerateObject().SelectMany(p => Flatten(p.Value, prefix == "" ? p.Name : prefix + "." + p.Name))
            : [(prefix, e)];

    void RowCounts(JsonElement counts)
    {
        var files = counts.EnumerateObject().ToList();
        foreach (var f in files.Where(f => !RowCountQueries.ContainsKey(f.Name)))
            Fail($"{f.Name}: no model table is mapped to this CSV file");
        var mapped = files.Where(f => RowCountQueries.ContainsKey(f.Name)).ToList();
        planned = files.Count;
        var row = Query("EVALUATE ROW ( " + string.Join(", ", mapped.Select((f, i) => $"\"c{i}\", {RowCountQueries[f.Name]}")) + " )")[0];
        for (var i = 0; i < mapped.Count; i++)
            Compare($"{mapped[i].Name}: rows", row[i], mapped[i].Value, Kind.Count, blankIsZero: true);
    }

    void Totals(JsonElement totals)
    {
        var all = totals.EnumerateObject().ToList();
        // Every measure of the model must have an expected value, and the other way round
        foreach (var m in modelMeasures.Where(m => !totals.TryGetProperty(m, out _)).Order())
            Fail($"[{m}] is in the model but has no expected value");
        foreach (var m in all.Where(m => !modelMeasures.Contains(m.Name)))
            Fail($"[{m.Name}] is not a measure of the model");
        var items = all.Where(m => modelMeasures.Contains(m.Name)).ToList();
        planned = all.Count;
        var row = Query("EVALUATE ROW ( " + string.Join(", ", items.Select((m, i) => $"\"c{i}\", {QuoteName(m.Name)}")) + " )")[0];
        Write($"  {"Measure",-30} {"Model",18} {"Expected",18}");
        for (var i = 0; i < items.Count; i++)
        {
            var kind = KindOf(items[i].Name, items[i].Value);
            var ok = Compare(items[i].Name, row[i], items[i].Value, kind, blankIsZero: true);
            Write($"  {items[i].Name,-30} {Show(row[i], kind),18} {Show(items[i].Value, kind),18}{(ok ? "" : "  <-- FAIL")}");
        }
    }

    // One block per value of the filter column(s): { "<value>": { "<measure or column>": expected, ... } };
    // with several filter columns the key is "<value 1>|<value 2>". Each block is evaluated in the
    // filter context of its values only, like one cell of a visual with these slicers.
    void Grouped(JsonElement groups, List<string> filterColumns)
    {
        var refs = filterColumns.Select(ParseColumn).ToList();
        var table = refs[0].Table;
        var names = groups.EnumerateObject().SelectMany(g => g.Value.EnumerateObject().Select(p => p.Name)).Distinct().ToList();
        planned = groups.EnumerateObject().Sum(g => g.Value.EnumerateObject().Count());
        var attributes = names.Where(n => !modelMeasures.Contains(n)).ToList();
        if (refs.Count > 1 && attributes.Count > 0)
        {
            Fail("attribute columns (" + string.Join(", ", attributes) + ") need a single filter column", planned);
            return;
        }

        // Measures of the model, or else columns of the filter table that must have one value per group (attributes)
        var exprs = names.Select(n => modelMeasures.Contains(n)
            ? QuoteName(n)
            : $"CALCULATE ( SELECTEDVALUE ( {QuoteTable(table)}{QuoteName(n)} ) )").ToList();
        var values = refs.Select(r => $"VALUES ( {QuoteTable(r.Table)}{QuoteName(r.Column)} )").ToList();
        var rows = values.Count == 1 ? values[0] : $"CROSSJOIN ( {string.Join(", ", values)} )";
        var dax = $"EVALUATE ADDCOLUMNS ( {rows}, " + string.Join(", ", exprs.Select((e, i) => $"\"c{i}\", {e}")) + " )";
        var n0 = refs.Count;
        var label = string.Join(" x ", filterColumns);

        var model = new Dictionary<string, object?[]>();
        foreach (var r in Query(dax)) model[string.Join("|", r.Take(n0).Select(Engine.Format))] = r;

        foreach (var g in groups.EnumerateObject())
        {
            if (!model.Remove(g.Name, out var row))
            {
                Fail($"{g.Name}: not a value of {label} in the model", g.Value.EnumerateObject().Count());
                continue;
            }
            foreach (var p in g.Value.EnumerateObject())
            {
                var i = names.IndexOf(p.Name);
                Compare($"{g.Name} / {p.Name}", row[i + n0], p.Value, KindOf(p.Name, p.Value), blankIsZero: true);
            }
        }

        // Values the generator does not know (such as the blank row of a dimension, or an owner
        // without orders) must be empty
        foreach (var (key, row) in model)
        {
            var filled = names.Select((n, i) => (n, v: row[i + n0])).Where(x => !IsEmpty(x.v)).ToList();
            if (filled.Count > 0)
                Fail($"{(key.Trim('|') == "" ? "(blank)" : key)}: unexpected value of {label} with " +
                     string.Join(", ", filled.Select(x => $"{x.n} = {Show(x.v, Kind.Text)}")));
        }
    }

    // A list of rows keyed on the first column; the model must have exactly these rows
    void Rows(JsonElement columnsJson, JsonElement rows, string tableExpr, Func<string, string?> columnExpr)
    {
        var columns = columnsJson.EnumerateArray().Select(c => c.GetString()!).ToList();
        planned = rows.GetArrayLength();
        var unmapped = columns.Where(c => columnExpr(c) == null).ToList();
        if (unmapped.Count > 0)
        {
            Fail("no DAX column mapped to " + string.Join(", ", unmapped), planned);
            return;
        }
        var dax = $"EVALUATE SELECTCOLUMNS ( {tableExpr}, " +
                  string.Join(", ", columns.Select((c, i) => $"\"c{i}\", {columnExpr(c)}")) + " )";

        var model = new Dictionary<string, object?[]>();
        foreach (var r in Query(dax))
        {
            var key = Engine.Format(r[0]);
            if (!model.TryAdd(key, r)) Fail($"{key}: key appears more than once in the model");
        }

        foreach (var expected in rows.EnumerateArray())
        {
            var cells = expected.EnumerateArray().ToArray();
            var key = cells[0].GetString()!;
            if (!model.Remove(key, out var row))
            {
                Fail($"{key}: row missing in the model");
                continue;
            }
            // Columns are data, not aggregates: blank and 0 are different here
            var wrong = Enumerable.Range(1, columns.Count - 1)
                .Where(i => Mismatch(row[i], cells[i], KindOf(columns[i], cells[i]), blankIsZero: false) != null)
                .Select(i => $"{columns[i]}: model {Show(row[i], Kind.Text)}, expected {Show(cells[i], Kind.Text)}")
                .ToList();
            if (wrong.Count == 0) Pass();
            else Fail($"{key}: " + string.Join("; ", wrong));
        }
        foreach (var key in model.Keys) Fail($"{key}: row in the model but not expected");
    }

    // ------------------------------------------------------------------ comparison

    Kind KindOf(string name, JsonElement want) =>
        moneyMeasures.Contains(name) ? Kind.Money
        : rateMeasures.Contains(name) ? Kind.Rate
        : want.ValueKind != JsonValueKind.Number ? Kind.Text
        // Python writes int as 12 and float as 12.0
        : want.GetRawText().IndexOfAny(['.', 'e', 'E']) < 0 ? Kind.Count
        : Kind.Decimal;

    bool Compare(string label, object? got, JsonElement want, Kind kind, bool blankIsZero)
    {
        var problem = Mismatch(got, want, kind, blankIsZero);
        if (problem == null)
        {
            Pass();
            return true;
        }
        Fail($"{label}: model {Show(got, kind)}, expected {Show(want, kind)}{problem}");
        return false;
    }

    // null when the model value matches; else a short note (may be empty)
    string? Mismatch(object? got, JsonElement want, Kind kind, bool blankIsZero)
    {
        var blank = got is null or DBNull;
        switch (want.ValueKind)
        {
            case JsonValueKind.Null:
                // DIVIDE by zero or an average over no rows: DAX returns BLANK
                return blank ? null : "";
            case JsonValueKind.Number:
                var w = want.GetDouble();
                // A sum or count over no rows is BLANK in DAX and 0 in Python
                if (blank) return blankIsZero && w == 0 ? null : "";
                if (!TryNumber(got, out var g)) return " (not a number)";
                var diff = Math.Abs(g - w);
                largestDiff[kind] = Math.Max(largestDiff.GetValueOrDefault(kind), diff);
                return diff <= tolerance.GetValueOrDefault(kind) ? null : string.Create(Inv, $" (diff {g - w:+0.######;-0.######})");
            case JsonValueKind.True or JsonValueKind.False:
                return got is bool b && b == want.GetBoolean() ? null : "";
            default:
                // Text, and dates as yyyy-mm-dd
                return !blank && Engine.Format(got) == want.GetString() ? null : "";
        }
    }

    static bool TryNumber(object? v, out double d)
    {
        switch (v)
        {
            case double x: d = x; return true;
            case decimal x: d = (double)x; return true;
            case long or int or short or byte or float:
                d = Convert.ToDouble(v, Inv);
                return true;
            default:
                d = 0;
                return false;
        }
    }

    static bool IsEmpty(object? v) => v is null or DBNull || (TryNumber(v, out var d) && d == 0) || v is "";

    static string Show(object? v, Kind kind)
    {
        if (v is null or DBNull) return "BLANK";
        if (!TryNumber(v, out var d)) return Engine.Format(v);
        return kind switch
        {
            Kind.Money => d.ToString("#,0.00", Inv),
            Kind.Count => d.ToString("#,0", Inv),
            Kind.Rate => d.ToString("0.0000%", Inv),
            _ => d.ToString("0.######", Inv),
        };
    }

    static string Show(JsonElement e, Kind kind) => e.ValueKind switch
    {
        JsonValueKind.Null => "BLANK",
        JsonValueKind.Number => Show(e.GetDouble(), kind),
        JsonValueKind.String => e.GetString()!,
        _ => e.GetRawText(),
    };

    // ------------------------------------------------------------------ DAX helpers

    List<object?[]> Query(string dax)
    {
        using var cmd = new AdomdCommand(dax, conn) { CommandTimeout = 300 };
        using var reader = cmd.ExecuteReader();
        var rows = new List<object?[]>();
        while (reader.Read())
        {
            var row = new object?[reader.FieldCount];
            reader.GetValues(row!);
            rows.Add(row);
        }
        return rows;
    }

    // "'Date'[Month Start]" or "Orders[Order No]" -> (table, column)
    static (string Table, string Column) ParseColumn(string reference)
    {
        var m = ColumnPattern().Match(reference);
        if (!m.Success) throw new FormatException($"not a DAX column reference: {reference}");
        return (m.Groups["t"].Value.Replace("''", "'"), m.Groups["c"].Value.Replace("]]", "]"));
    }

    [GeneratedRegex(@"^(?:'(?<t>(?:[^']|'')+)'|(?<t>[^'\[\]]+))\[(?<c>(?:[^\]]|\]\])+)\]$")]
    private static partial Regex ColumnPattern();

    static string QuoteTable(string name) => "'" + name.Replace("'", "''") + "'";
    static string QuoteName(string name) => "[" + name.Replace("]", "]]") + "]";
}
