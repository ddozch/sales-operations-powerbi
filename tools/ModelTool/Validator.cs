using System.Text.RegularExpressions;
using TOM = Microsoft.AnalysisServices.Tabular;

namespace ModelTool;

// Loads the TMDL folder with the same serializer Power BI uses and checks the
// modelling rules of this project. Every rule that fails prints one line.
static class Validator
{
    const string MeasureTable = "_Measures";

    public static int Run(string folder, bool list)
    {
        TOM.Database db;
        try
        {
            db = TOM.TmdlSerializer.DeserializeDatabaseFromFolder(folder);
        }
        catch (Exception ex)
        {
            Console.WriteLine("TMDL does not load: " + ex.Message);
            return 1;
        }

        var model = db.Model;
        var problems = new List<string>();
        var relationships = model.Relationships.OfType<TOM.SingleColumnRelationship>().ToList();
        var foreignKeys = relationships.Select(r => r.FromColumn).ToHashSet();

        CheckMeasures(model, problems);
        CheckKeys(model, foreignKeys, problems);
        CheckRelationships(model, relationships, problems);
        CheckDateTable(model, problems);
        CheckDataFolder(model, folder, problems);
        CheckFirewall(model, problems);

        var columns = model.Tables.Sum(t => t.Columns.Count(c => c.Type != TOM.ColumnType.RowNumber));
        var measures = model.Tables.Sum(t => t.Measures.Count);
        Console.WriteLine($"TMDL loads: compatibility level {db.CompatibilityLevel}, {model.Tables.Count} tables, " +
                          $"{columns} columns, {measures} measures, {relationships.Count} relationships, {model.Expressions.Count} expressions");
        if (list) PrintList(model, relationships);

        if (problems.Count == 0)
        {
            Console.WriteLine("Model rules: all passed");
            return 0;
        }
        Console.WriteLine($"Model rules: {problems.Count} violation(s)");
        foreach (var p in problems) Console.WriteLine("  - " + p);
        return 1;
    }

    static void CheckMeasures(TOM.Model model, List<string> problems)
    {
        if (model.Tables.Find(MeasureTable) is not { } home)
            problems.Add($"no table named '{MeasureTable}' for the measures");
        else if (home.Columns.Any(c => c.Type != TOM.ColumnType.RowNumber && !c.IsHidden))
            problems.Add($"'{MeasureTable}' has visible columns, so Power BI does not show it as a measure table");
        // TOM accepts it, but Power BI Desktop refuses to open a model with a table named "Measures"
        if (model.Tables.Any(t => t.Name.Equals("Measures", StringComparison.OrdinalIgnoreCase)))
            problems.Add("a table is named 'Measures', a reserved name in Power BI Desktop");

        foreach (var t in model.Tables)
        foreach (var m in t.Measures)
        {
            var name = $"[{m.Name}]";
            if (t.Name != MeasureTable) problems.Add($"{name} is in table '{t.Name}', not in '{MeasureTable}'");
            if (string.IsNullOrWhiteSpace(m.Description)) problems.Add($"{name} has no description");
            if (string.IsNullOrWhiteSpace(m.DisplayFolder)) problems.Add($"{name} has no display folder");
            if (string.IsNullOrWhiteSpace(m.FormatString) && m.FormatStringDefinition == null)
                problems.Add($"{name} has no format string");
        }
    }

    // Keys are the "... Key" columns and every column on the many side of a relationship
    static void CheckKeys(TOM.Model model, HashSet<TOM.Column> foreignKeys, List<string> problems)
    {
        foreach (var t in model.Tables)
        foreach (var c in t.Columns.Where(c => c.Type != TOM.ColumnType.RowNumber))
        {
            var isKey = c.Name.EndsWith(" Key", StringComparison.OrdinalIgnoreCase) || foreignKeys.Contains(c);
            if (!isKey) continue;
            if (!c.IsHidden) problems.Add($"key column '{t.Name}'[{c.Name}] is not hidden");
            if (c.SummarizeBy != TOM.AggregateFunction.None) problems.Add($"key column '{t.Name}'[{c.Name}] has summarizeBy {c.SummarizeBy}, not none");
        }
    }

    static void CheckRelationships(TOM.Model model, List<TOM.SingleColumnRelationship> relationships, List<string> problems)
    {
        if (relationships.Count != model.Relationships.Count)
            problems.Add("the model has relationships that are not single-column relationships");
        foreach (var r in relationships)
        {
            var name = Describe(r);
            if (r.CrossFilteringBehavior == TOM.CrossFilteringBehavior.BothDirections)
                problems.Add($"{name} filters in both directions");
            if (r.FromCardinality != TOM.RelationshipEndCardinality.Many || r.ToCardinality != TOM.RelationshipEndCardinality.One)
                problems.Add($"{name} is {r.FromCardinality}-to-{r.ToCardinality}, not many-to-one");
        }
    }

    // Marked date table, and no auto date/time (hidden LocalDateTable_* tables and column variations)
    static void CheckDateTable(TOM.Model model, List<string> problems)
    {
        var dateTables = model.Tables.Where(t => t.DataCategory == "Time" &&
            t.Columns.Any(c => c.IsKey && c.DataType == TOM.DataType.DateTime)).ToList();
        if (dateTables.Count == 0) problems.Add("no table is marked as date table (dataCategory Time with a date key column)");

        if (model.Annotations.Find("__PBI_TimeIntelligenceEnabled")?.Value != "0")
            problems.Add("auto date/time is not off (annotation __PBI_TimeIntelligenceEnabled = 0 is missing)");
        foreach (var t in model.Tables.Where(t => t.Name.StartsWith("LocalDateTable_") || t.Name.StartsWith("DateTableTemplate_")))
            problems.Add($"auto date/time table '{t.Name}' exists");
        foreach (var t in model.Tables)
        foreach (var c in t.Columns.Where(c => c.Variations.Count > 0))
            problems.Add($"'{t.Name}'[{c.Name}] has date variations (auto date/time)");
    }

    // The data folder is a text parameter with an absolute path ending with a backslash. A path to another
    // folder is allowed (set_data_folder.ps1 -Path), but it is reported: after a copy or a clone the old
    // path may still exist, and Desktop would quietly load another folder's CSV files.
    static void CheckDataFolder(TOM.Model model, string definitionFolder, List<string> problems)
    {
        var e = model.Expressions.Find("DataFolder");
        var m = e == null ? null : Regex.Match(e.Expression, "^\\s*\"((?:[^\"]|\"\")*)\"\\s+meta\\s*\\[");
        if (m is not { Success: true })
        {
            problems.Add("parameter DataFolder (text, with meta IsParameterQuery) not found");
            return;
        }
        var path = m.Groups[1].Value.Replace("\"\"", "\"");
        var own = Path.GetFullPath(Path.Combine(definitionFolder, "..", "..", "data")).TrimEnd('\\') + "\\";
        if (!Path.IsPathRooted(path) || !path.EndsWith('\\'))
            problems.Add($"DataFolder '{path}' is not an absolute path ending with a backslash");
        else if (!Directory.Exists(path))
            Console.WriteLine($"Note: DataFolder '{path}' does not exist on this PC (run tools\\set_data_folder.ps1)");
        else if (!string.Equals(Path.GetFullPath(path), own, StringComparison.OrdinalIgnoreCase))
            Console.WriteLine($"Warning: DataFolder '{path}' is not the data folder of this project ({own}); " +
                              "run tools\\set_data_folder.ps1 unless another export is meant");
    }

    // Power Query's formula firewall: with privacy levels on, a query may either read a data source or
    // combine other queries, not both. Functions are inlined into the query that calls them, so calling
    // fnReadCsv counts as reading. Checked offline here because the model sets fastCombine (privacy levels
    // ignored) and a refresh on this PC would never show the problem.
    static readonly string[] SourceFunctions = ["File.Contents", "Csv.Document", "Folder.Files", "Folder.Contents",
        "Excel.Workbook", "Web.Contents", "Sql.Database", "OData.Feed", "CommonDataService.Database", "Dataverse.Contents"];

    static void CheckFirewall(TOM.Model model, List<string> problems)
    {
        var queries = new Dictionary<string, string>();
        foreach (var e in model.Expressions) queries[e.Name] = e.Expression;
        foreach (var t in model.Tables)
        foreach (var p in t.Partitions)
            if (p.Source is TOM.MPartitionSource ms) queries[$"partition '{p.Name}'"] = ms.Expression;

        bool IsFunction(string name) => model.Expressions.Find(name) is { } e &&
            (e.Annotations.Find("PBI_ResultType")?.Value == "Function" || Regex.IsMatch(e.Expression, @"^\s*\([^)]*\)[^=]*=>"));
        IEnumerable<string> References(string text) => model.Expressions.Select(e => e.Name)
            .Where(n => Regex.IsMatch(text, $@"(?<![\w.#""]){Regex.Escape(n)}(?![\w""])|#""{Regex.Escape(n)}"""));

        // Reads a source itself, or through a function it calls (transitively)
        var reads = new Dictionary<string, bool>();
        bool ReadsDirectly(string name, HashSet<string> seen)
        {
            if (reads.TryGetValue(name, out var known)) return known;
            if (!seen.Add(name)) return false;
            var text = queries[name];
            var result = SourceFunctions.Any(f => text.Contains(f + "(", StringComparison.Ordinal)) ||
                         References(text).Any(r => r != name && IsFunction(r) && ReadsDirectly(r, seen));
            reads[name] = result;
            return result;
        }
        // Reads a source anywhere below it (directly or through referenced queries)
        bool ReadsAnywhere(string name, HashSet<string> seen) =>
            seen.Add(name) && (ReadsDirectly(name, []) ||
                               References(queries[name]).Any(r => r != name && ReadsAnywhere(r, seen)));

        foreach (var (name, text) in queries)
        {
            if (IsFunction(name) || !ReadsDirectly(name, [])) continue;
            foreach (var r in References(text).Where(r => r != name && !IsFunction(r) && ReadsAnywhere(r, [])))
                problems.Add($"{(name.StartsWith("partition") ? name : $"query '{name}'")} reads a data source and also uses " +
                             $"query '{r}', which reads one too: with privacy levels on, the refresh fails with Formula.Firewall");
        }
    }

    static string Describe(TOM.SingleColumnRelationship r) =>
        $"'{r.FromTable.Name}'[{r.FromColumn.Name}] -> '{r.ToTable.Name}'[{r.ToColumn.Name}]";

    static void PrintList(TOM.Model model, List<TOM.SingleColumnRelationship> relationships)
    {
        Console.WriteLine();
        Console.WriteLine("Tables:");
        foreach (var t in model.Tables)
        {
            var cols = t.Columns.Where(c => c.Type != TOM.ColumnType.RowNumber)
                .Select(c => c.Name + (c.IsHidden ? "*" : "")).ToList();
            var tag = t.DataCategory == "Time" ? " (date table)" : "";
            Console.WriteLine($"  {t.Name}{tag}: {string.Join(", ", cols)}");
        }
        Console.WriteLine("  (* = hidden)");
        Console.WriteLine("Relationships (many-to-one, single direction):");
        foreach (var r in relationships)
            Console.WriteLine($"  {Describe(r)}{(r.IsActive ? "" : "  [inactive]")}");
        Console.WriteLine("Measures:");
        foreach (var m in model.Tables.SelectMany(t => t.Measures).OrderBy(m => m.DisplayFolder))
            Console.WriteLine($"  {m.DisplayFolder} | {m.Name} | {m.FormatString}");
    }
}
