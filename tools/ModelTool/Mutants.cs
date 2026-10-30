using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using TOM = Microsoft.AnalysisServices.Tabular;

namespace ModelTool;

// Mutation run: shows that the golden-master test fails when the model is wrong. Each mutant makes one
// deliberate mistake in the M or DAX of the model deployed in a running Power BI Desktop (find/replace
// on the deployed text; the TMDL files are never touched), refreshes when M changed, runs the whole
// test quietly and expects failures. The original text is put back after each mutant; at the end the
// model is refreshed and must pass the test again. Desktop must not be saved afterwards.
static class Mutants
{
    sealed record Mutant(string Name, string Kind, string Target, string Find, string Replace);

    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    public static int Run(string port, string mutantsFile, string expectedFile, int waitSeconds)
    {
        var watch = Stopwatch.StartNew();
        var mutants = JsonSerializer.Deserialize<List<Mutant>>(File.ReadAllText(mutantsFile),
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true })!;
        using var doc = JsonDocument.Parse(File.ReadAllBytes(expectedFile));
        var root = doc.RootElement;
        var (server, db) = Engine.WaitForModel(port, waitSeconds);
        using var _ = server;

        // Every mutant must still match the model text exactly once, or it tests nothing
        var stale = mutants.Select(m => (m, n: Occurrences(Text(db.Model, m).Get(), m.Find))).Where(x => x.n != 1).ToList();
        foreach (var (m, n) in stale) Console.WriteLine($"ERROR {m.Name}: the find text occurs {n} times in {m.Kind} '{m.Target}'");
        if (stale.Count > 0) return 1;

        var baseline = Test(port, root);
        Console.WriteLine($"Mutation run on localhost:{port}: {mutants.Count} mutants");
        Console.WriteLine($"Baseline: {baseline.Passed} passed, {baseline.Failed} failed");
        if (baseline.Failed > 0)
        {
            Console.WriteLine("The model must pass the test before the mutation run");
            return 1;
        }
        Console.WriteLine();

        // Measures need no refresh, so they run first, on the original data
        var survived = 0;
        var dataChanged = false;
        foreach (var m in mutants.OrderBy(m => m.Kind == "measure" ? 0 : 1))
        {
            var text = Text(db.Model, m);
            var original = text.Get();
            text.Set(original.Replace(m.Find, m.Replace, StringComparison.Ordinal));
            db.Model.SaveChanges();
            string outcome;
            var refreshErrors = new List<string>();
            if (m.Kind != "measure")
            {
                refreshErrors = Engine.RefreshModel(db).Errors;
                dataChanged = true;
            }
            if (refreshErrors.Count > 0)
            {
                outcome = "killed: refresh fails (" + Short(refreshErrors[0]) + ")";
            }
            else
            {
                var r = Test(port, root);
                outcome = r.Failed > 0
                    ? $"killed: {r.Failed} checks fail in " + string.Join(", ", r.Sections.Select(s => $"{s.Name} {s.Failed}"))
                    : "SURVIVED: every check passes";
                if (r.Failed == 0) survived++;
            }
            Console.WriteLine($"- {m.Name}");
            Console.WriteLine($"    {outcome}");

            text.Set(original);
            db.Model.SaveChanges();
        }

        if (dataChanged)
        {
            var errors = Engine.RefreshModel(db).Errors;
            if (errors.Count > 0)
            {
                Console.WriteLine("ERROR: the restored model does not refresh: " + errors[0]);
                return 1;
            }
        }
        var after = Test(port, root);
        Console.WriteLine();
        Console.WriteLine($"Restored model: {after.Passed} passed, {after.Failed} failed");
        Console.WriteLine(string.Create(Inv,
            $"MUTANTS: {mutants.Count - survived} killed, {survived} survived ({watch.Elapsed.TotalSeconds:0} s)"));
        return survived == 0 && after.Failed == 0 ? 0 : 1;
    }

    // A fresh connection per run: the model metadata changes between runs
    static (int Passed, int Failed, List<(string Name, int Failed)> Sections) Test(string port, JsonElement root)
    {
        using var conn = Engine.Open(port);
        return Tester.RunQuiet(conn, root);
    }

    static (Func<string> Get, Action<string> Set) Text(TOM.Model model, Mutant m)
    {
        switch (m.Kind)
        {
            case "expression":
                var e = model.Expressions.Find(m.Target) ?? throw new ArgumentException($"no expression '{m.Target}'");
                return (() => e.Expression, v => e.Expression = v);
            case "partition":
                var t = model.Tables.Find(m.Target) ?? throw new ArgumentException($"no table '{m.Target}'");
                var p = t.Partitions[0].Source as TOM.MPartitionSource ?? throw new ArgumentException($"'{m.Target}' has no M partition");
                return (() => p.Expression, v => p.Expression = v);
            case "measure":
                var ms = model.Tables.SelectMany(x => x.Measures).FirstOrDefault(x => x.Name == m.Target)
                         ?? throw new ArgumentException($"no measure '{m.Target}'");
                return (() => ms.Expression, v => ms.Expression = v);
            default:
                throw new ArgumentException($"unknown kind '{m.Kind}' (expression, partition or measure)");
        }
    }

    static int Occurrences(string text, string find)
    {
        var n = 0;
        for (var i = text.IndexOf(find, StringComparison.Ordinal); i >= 0; i = text.IndexOf(find, i + 1, StringComparison.Ordinal)) n++;
        return n;
    }

    static string Short(string message)
    {
        var line = message.Split('\n')[0].Trim();
        return line.Length <= 160 ? line : line[..157] + "...";
    }
}
