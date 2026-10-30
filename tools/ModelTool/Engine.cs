using System.Diagnostics;
using System.Globalization;
using System.Text;
using Microsoft.AnalysisServices.AdomdClient;
using AS = Microsoft.AnalysisServices;
using TOM = Microsoft.AnalysisServices.Tabular;

namespace ModelTool;

// Refresh and DAX queries against the local engine of Power BI Desktop (XMLA on localhost:<port>).
static class Engine
{
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    public static string ConnectionString(string port) => $"Data Source=localhost:{int.Parse(port, Inv)}";

    public static int Refresh(string port, int waitSeconds)
    {
        var (server, db) = WaitForModel(port, waitSeconds);
        using var _ = server;
        var watch = Stopwatch.StartNew();
        var (errors, messages) = RefreshModel(db);
        foreach (var m in messages) Console.WriteLine("message: " + m);
        foreach (var e in errors) Console.WriteLine("ERROR: " + e);
        if (errors.Count > 0) return 1;
        Console.WriteLine(string.Create(Inv, $"Refresh done in {watch.Elapsed.TotalSeconds:0.0} s"));

        // Row counts show at once whether every table loaded
        var tables = db.Model.Tables.Select(t => t.Name).ToList();
        var dax = "EVALUATE UNION(" + string.Join(", ", tables.Select(t =>
            $"ROW(\"Table\", \"{t}\", \"Rows\", COUNTROWS('{t.Replace("'", "''")}') + 0)")) + ")";
        using var conn = Open(port);
        using var cmd = new AdomdCommand(dax, conn);
        using var reader = cmd.ExecuteReader();
        while (reader.Read())
            Console.WriteLine(string.Create(Inv, $"  {reader.GetString(0),-16} {Convert.ToInt64(reader.GetValue(1), Inv),8:N0}"));
        return 0;
    }

    // Full refresh of the whole model: (errors, other messages). Mashup (M) errors arrive as an
    // OperationException with one message per failing partition.
    public static (List<string> Errors, List<string> Messages) RefreshModel(TOM.Database db)
    {
        var errors = new List<string>();
        var messages = new List<string>();
        try
        {
            db.Model.RequestRefresh(TOM.RefreshType.Full);
            var result = db.Model.SaveChanges();
            foreach (var r in result.XmlaResults?.Cast<AS.XmlaResult>() ?? [])
            foreach (AS.XmlaMessage msg in r.Messages)
                (msg is AS.XmlaError ? errors : messages).Add(msg.Description);
        }
        catch (AS.OperationException ex)
        {
            foreach (AS.XmlaResult r in ex.Results)
            foreach (AS.XmlaMessage msg in r.Messages)
                errors.Add(msg.Description);
            if (errors.Count == 0) errors.Add(ex.Message);
            // The failed refresh leaves pending changes in the local model; drop them
            db.Model.UndoLocalChanges();
        }
        return (errors, messages);
    }

    // Desktop writes the port file before it has deployed the model from TMDL, so wait for the tables.
    // Each attempt is bounded: a connect can block while Desktop is busy or shows an error dialog.
    public static (TOM.Server, TOM.Database) WaitForModel(string port, int waitSeconds)
    {
        var deadline = DateTime.UtcNow.AddSeconds(waitSeconds);
        while (DateTime.UtcNow < deadline)
        {
            var server = new TOM.Server();
            var attempt = Task.Run(() =>
            {
                server.Connect(ConnectionString(port) + ";Connect Timeout=15");
                return server.Databases.Count > 0 && server.Databases[0].Model is { Tables.Count: > 0 };
            });
            try
            {
                if (attempt.Wait(TimeSpan.FromSeconds(30)) && attempt.Result) return (server, server.Databases[0]);
            }
            catch (AggregateException)
            {
                // engine not ready yet
            }
            // A blocked attempt keeps its server; it is abandoned, not reused
            if (attempt.IsCompleted) server.Dispose();
            Thread.Sleep(2000);
        }
        throw new TimeoutException($"no model on localhost:{port} after {waitSeconds} s");
    }

    public static AdomdConnection Open(string port)
    {
        var conn = new AdomdConnection(ConnectionString(port));
        conn.Open();
        return conn;
    }

    public static int Query(string port, string daxOrFile, bool types)
    {
        var dax = daxOrFile.StartsWith('@') ? File.ReadAllText(daxOrFile[1..], Encoding.UTF8) : daxOrFile;
        using var conn = Open(port);
        using var cmd = new AdomdCommand(dax, conn) { CommandTimeout = 300 };
        try
        {
            using var reader = cmd.ExecuteReader();
            do
            {
                var names = Enumerable.Range(0, reader.FieldCount).Select(reader.GetName);
                Console.WriteLine(string.Join("\t", names));
                if (types)
                    Console.WriteLine(string.Join("\t", Enumerable.Range(0, reader.FieldCount).Select(i => reader.GetFieldType(i).Name)));
                while (reader.Read())
                    Console.WriteLine(string.Join("\t", Enumerable.Range(0, reader.FieldCount).Select(i => Format(reader.GetValue(i)))));
            } while (reader.NextResult());
        }
        catch (AdomdException ex)
        {
            Console.WriteLine("ERROR: " + ex.Message);
            return 1;
        }
        return 0;
    }

    // Culture-independent text: ISO dates, "." decimals, empty for blank
    public static string Format(object? value) => value switch
    {
        null or DBNull => "",
        DateTime d when d.TimeOfDay == TimeSpan.Zero => d.ToString("yyyy-MM-dd", Inv),
        DateTime d => d.ToString("yyyy-MM-ddTHH:mm:ss", Inv),
        double x => x.ToString("R", Inv),
        bool b => b ? "TRUE" : "FALSE",
        IFormattable f => f.ToString(null, Inv),
        _ => value.ToString() ?? "",
    };
}
