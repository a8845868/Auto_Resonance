using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace AutoResonance.StableLauncher
{
    internal static class Program
    {
        private const string TargetFileName = "launcher-target.txt";
        private const string StartScriptName = "start-gui.cmd";
        private const string PythonLauncherName = "gui_launcher.pyw";

        [STAThread]
        private static int Main(string[] args)
        {
            string launcherDirectory = AppDomain.CurrentDomain.BaseDirectory;
            string targetFile = Path.Combine(launcherDirectory, TargetFileName);
            bool validateOnly = args.Length == 1 &&
                string.Equals(args[0], "--validate-only", StringComparison.OrdinalIgnoreCase);

            try
            {
                string targetRoot = ReadTargetRoot(targetFile);
                ValidateTarget(targetRoot);
                if (validateOnly)
                {
                    return 0;
                }

                string script = Path.Combine(targetRoot, StartScriptName);
                string commandProcessor = Environment.GetEnvironmentVariable("ComSpec");
                if (string.IsNullOrWhiteSpace(commandProcessor))
                {
                    commandProcessor = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.System),
                        "cmd.exe"
                    );
                }

                ProcessStartInfo startInfo = new ProcessStartInfo();
                startInfo.FileName = commandProcessor;
                startInfo.Arguments = "/d /c call \"" + script + "\"";
                startInfo.WorkingDirectory = targetRoot;
                startInfo.UseShellExecute = false;
                startInfo.CreateNoWindow = true;
                Process process = Process.Start(startInfo);
                if (process == null)
                {
                    throw new InvalidOperationException("启动进程未创建");
                }
                process.Dispose();
                return 0;
            }
            catch (Exception error)
            {
                WriteFailureLog(launcherDirectory, error);
                if (!validateOnly)
                {
                    MessageBox.Show(
                        "黑月无人驾驶启动失败。\r\n\r\n" + error.Message +
                        "\r\n\r\n错误日志：\r\n" +
                        Path.Combine(launcherDirectory, "launcher-error.log"),
                        "黑月无人驾驶 - 启动失败",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                }
                return 2;
            }
        }

        private static string ReadTargetRoot(string targetFile)
        {
            if (!File.Exists(targetFile))
            {
                throw new FileNotFoundException("缺少启动目标配置", targetFile);
            }
            string value = File.ReadAllText(targetFile, Encoding.UTF8).Trim();
            value = value.Trim('"');
            if (value.Length == 0)
            {
                throw new InvalidDataException("启动目标配置为空");
            }
            return Path.GetFullPath(Environment.ExpandEnvironmentVariables(value));
        }

        private static void ValidateTarget(string targetRoot)
        {
            if (!Directory.Exists(targetRoot))
            {
                throw new DirectoryNotFoundException("GUI 目录不存在：" + targetRoot);
            }
            string script = Path.Combine(targetRoot, StartScriptName);
            string launcher = Path.Combine(targetRoot, PythonLauncherName);
            if (!File.Exists(script) || !File.Exists(launcher))
            {
                throw new InvalidDataException(
                    "目标目录不是有效的 Auto_Resonance GUI：" + targetRoot
                );
            }
        }

        private static void WriteFailureLog(string launcherDirectory, Exception error)
        {
            try
            {
                string line = DateTimeOffset.Now.ToString("O") + " " +
                    error.GetType().Name + ": " + error.Message + Environment.NewLine;
                File.AppendAllText(
                    Path.Combine(launcherDirectory, "launcher-error.log"),
                    line,
                    Encoding.UTF8
                );
            }
            catch
            {
                // A launcher error must not be replaced by a logging error.
            }
        }
    }
}
