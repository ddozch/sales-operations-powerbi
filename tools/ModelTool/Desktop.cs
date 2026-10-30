using System.Net.Sockets;
using System.Text;

namespace ModelTool;

// Power BI Desktop runs its own Analysis Services engine on a random port and writes
// the port to <workspace>\Data\msmdsrv.port.txt (UTF-16).
static class Desktop
{
    public static IEnumerable<string> WorkspaceRoots()
    {
        var profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        yield return Path.Combine(profile, "Microsoft", "Power BI Desktop Store App", "AnalysisServicesWorkspaces");
        yield return Path.Combine(local, "Microsoft", "Power BI Desktop", "AnalysisServicesWorkspaces");
    }

    public static int FindPort(bool all)
    {
        var candidates = WorkspaceRoots()
            .Where(Directory.Exists)
            .SelectMany(Directory.GetDirectories)
            .Select(d => Path.Combine(d, "Data", "msmdsrv.port.txt"))
            .Where(File.Exists)
            .Select(f => (File: f, Time: File.GetLastWriteTime(f), Port: ReadPort(f)))
            .Where(c => c.Port > 0)
            .OrderByDescending(c => c.Time)
            .ToList();

        // Port files of closed sessions stay behind; only a listening port counts
        int? found = null;
        foreach (var c in candidates)
        {
            var open = IsListening(c.Port);
            if (all) Console.WriteLine($"{c.Port}\t{(open ? "listening" : "closed")}\t{c.Time:yyyy-MM-dd HH:mm:ss}\t{c.File}");
            if (open && found == null) found = c.Port;
            if (!all && found != null) break;
        }
        if (found == null)
        {
            Console.Error.WriteLine("No running Power BI Desktop engine found");
            return 1;
        }
        if (!all) Console.WriteLine(found);
        return 0;
    }

    static int ReadPort(string file)
    {
        var digits = new string(File.ReadAllText(file, Encoding.Unicode).Where(char.IsAsciiDigit).ToArray());
        return int.TryParse(digits, out var port) ? port : 0;
    }

    static bool IsListening(int port)
    {
        try
        {
            using var client = new TcpClient();
            return client.ConnectAsync("localhost", port).Wait(TimeSpan.FromSeconds(1)) && client.Connected;
        }
        catch (Exception)
        {
            return false;
        }
    }
}
