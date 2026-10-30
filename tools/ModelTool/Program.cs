// ModelTool: checks the TMDL model offline and talks to the Analysis Services engine
// of a running Power BI Desktop (refresh, DAX queries).
using System.Text;
using ModelTool;

Console.OutputEncoding = new UTF8Encoding(false);

try
{
    return args switch
    {
        ["validate", var folder, ..] => Validator.Run(folder, args.Contains("--list")),
        ["find-port", ..] => Desktop.FindPort(args.Contains("--all")),
        ["refresh", var port, ..] => Engine.Refresh(port, OptionInt(args, "--wait", 180)),
        ["query", var port, var dax, ..] => Engine.Query(port, dax, args.Contains("--types")),
        ["test", var port, var expected, ..] => Tester.Run(port, expected, args.Contains("--all-fails")),
        ["mutants", var port, var mutants, var expected, ..] => Mutants.Run(port, mutants, expected, OptionInt(args, "--wait", 180)),
        _ => Usage(),
    };
}
catch (Exception ex)
{
    Console.Error.WriteLine("ERROR: " + ex.Message);
    return 2;
}

static int OptionInt(string[] args, string name, int fallback)
{
    var i = Array.IndexOf(args, name);
    return i >= 0 && i + 1 < args.Length && int.TryParse(args[i + 1], out var v) ? v : fallback;
}

static int Usage()
{
    Console.WriteLine("""
        ModelTool <command>
          validate <definition folder> [--list]   Load the TMDL and check the model rules; --list prints tables, relationships, measures
          find-port [--all]                        Port of the running Power BI Desktop engine (newest reachable workspace)
          refresh <port> [--wait <seconds>]        Wait for the model, full refresh, print row counts
          query <port> <dax | @file> [--types]     Run a DAX query, print tab-separated rows
          test <port> <expected.json> [--all-fails] Compare every figure of expected.json with a DAX query (golden master)
          mutants <port> <mutants.json> <expected.json>
                                                   Apply each deliberate mistake to the running model, refresh, expect the test to fail
        Exit code: 0 ok, 1 rule violations, refresh/query errors, test failures or a surviving mutant, 2 tool error.
        """);
    return 2;
}
