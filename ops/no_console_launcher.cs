// Build as winexe: neither this launcher nor its console child gets a console.
// File handles (never capture pipes) preserve independent service lifetimes.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Chanlun.Operations
{
    public sealed class NoConsoleChild : IDisposable
    {
        private readonly SafeWaitHandle handle;
        private readonly SafeFileHandle job;
        public int Id { get; private set; }
        internal NoConsoleChild(IntPtr value, int id, SafeFileHandle lifetimeJob)
        {
            handle = new SafeWaitHandle(value, true);
            job = lifetimeJob;
            Id = id;
        }
        public bool HasExited { get { return NoConsoleLauncher.Wait(handle, 0); } }
        public int ExitCode
        {
            get
            {
                if (!HasExited) throw new InvalidOperationException("Process has not exited.");
                uint value;
                if (!NoConsoleLauncher.GetExitCodeProcess(handle, out value))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                return unchecked((int)value);
            }
        }
        public ProcessPriorityClass PriorityClass
        {
            get { using (Process p = Process.GetProcessById(Id)) return p.PriorityClass; }
            set { using (Process p = Process.GetProcessById(Id)) p.PriorityClass = value; }
        }
        public void Refresh() { }
        public void WaitForExit() { NoConsoleLauncher.Wait(handle, UInt32.MaxValue); }
        public bool WaitForExit(int milliseconds)
        {
            if (milliseconds < -1) throw new ArgumentOutOfRangeException("milliseconds");
            return NoConsoleLauncher.Wait(handle, unchecked((uint)milliseconds));
        }
        public void Dispose()
        {
            handle.Dispose();
            if (job != null) job.Dispose();
        }
    }

    public static class NoConsoleLauncher
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int Length;
            public IntPtr Descriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool Inherit;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo
        {
            public int Size;
            public string Reserved, Desktop, Title;
            public int X, Y, XSize, YSize, XCount, YCount, FillAttribute, Flags;
            public short ShowWindow, ReservedSize;
            public IntPtr ReservedPointer, Input, Output, Error;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct StartupInfoEx
        {
            public StartupInfo Info;
            public IntPtr Attributes;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInformation
        {
            public IntPtr Process, Thread;
            public int ProcessId, ThreadId;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct BasicJobLimit
        {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedJobLimit
        {
            public BasicJobLimit Basic;
            public ulong ReadOperations, WriteOperations, OtherOperations;
            public ulong ReadBytes, WriteBytes, OtherBytes;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern SafeFileHandle CreateJobObject(IntPtr security, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(SafeFileHandle job, int informationClass,
            ref ExtendedJobLimit information, int length);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(SafeFileHandle job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint code);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share,
            ref SecurityAttributes security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool InitializeProcThreadAttributeList(IntPtr list, int count,
            int flags, ref IntPtr size);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags,
            IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);
        [DllImport("kernel32.dll")]
        private static extern void DeleteProcThreadAttributeList(IntPtr list);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcess(string application, StringBuilder command,
            IntPtr processSecurity, IntPtr threadSecurity, bool inheritHandles, uint flags,
            IntPtr environment, string directory, ref StartupInfoEx startup,
            out ProcessInformation process);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(SafeWaitHandle handle, uint timeout);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern bool GetExitCodeProcess(SafeWaitHandle handle, out uint code);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        internal static bool Wait(SafeWaitHandle handle, uint milliseconds)
        {
            uint result = WaitForSingleObject(handle, milliseconds);
            if (result == 0) return true;
            if (result == 258) return false;
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }

        private static SafeFileHandle OpenFile(string path, bool input)
        {
            var security = new SecurityAttributes {
                Length = Marshal.SizeOf(typeof(SecurityAttributes)), Inherit = true
            };
            // FILE_APPEND_DATA prevents concurrent launches from overwriting logs.
            var file = CreateFile(path, input ? 0x80000000U : 4U, 7, ref security,
                input ? 3U : 4U, 0x80, IntPtr.Zero);
            if (file.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error(), path);
            return file;
        }

        public static NoConsoleChild Start(string executable, string arguments,
            string workingDirectory, string stdoutPath, string stderrPath)
        {
            return StartCore(executable, arguments, workingDirectory, stdoutPath, stderrPath, false, true);
        }

        private static NoConsoleChild StartCore(string executable, string arguments,
            string workingDirectory, string stdoutPath, string stderrPath, bool supervised, bool keepDescendants)
        {
            if (!Path.IsPathRooted(executable) || executable.IndexOf('"') >= 0)
                throw new ArgumentException("Executable must be an absolute, unquoted path.");
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(stdoutPath)));
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(stderrPath)));
            using (var input = OpenFile("NUL", true))
            using (var output = OpenFile(stdoutPath, false))
            using (var error = OpenFile(stderrPath, false))
            {
                IntPtr size = IntPtr.Zero, list = IntPtr.Zero, values = IntPtr.Zero;
                bool initialized = false;
                try
                {
                    InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
                    list = Marshal.AllocHGlobal(size);
                    if (!InitializeProcThreadAttributeList(list, 1, 0, ref size))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    initialized = true;
                    values = Marshal.AllocHGlobal(IntPtr.Size * 3);
                    Marshal.WriteIntPtr(values, 0, input.DangerousGetHandle());
                    Marshal.WriteIntPtr(values, IntPtr.Size, output.DangerousGetHandle());
                    Marshal.WriteIntPtr(values, IntPtr.Size * 2, error.DangerousGetHandle());
                    // Inherit only these three files, never the caller's capture pipes.
                    if (!UpdateProcThreadAttribute(list, 0, new IntPtr(0x20002), values,
                        new IntPtr(IntPtr.Size * 3), IntPtr.Zero, IntPtr.Zero))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    var startup = new StartupInfoEx();
                    startup.Info.Size = Marshal.SizeOf(typeof(StartupInfoEx));
                    startup.Info.Flags = 0x100; // STARTF_USESTDHANDLES
                    startup.Info.Input = input.DangerousGetHandle();
                    startup.Info.Output = output.DangerousGetHandle();
                    startup.Info.Error = error.DangerousGetHandle();
                    startup.Attributes = list;
                    ProcessInformation process;
                    var command = new StringBuilder("\"" + executable + "\" " + arguments);
                    // CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED.
                    if (!CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, true,
                        0x08080004, IntPtr.Zero, workingDirectory, ref startup, out process))
                        throw new Win32Exception(Marshal.GetLastWin32Error(), executable);
                    SafeFileHandle job = null;
                    try
                    {
                        if (supervised)
                        {
                            job = CreateJobObject(IntPtr.Zero, null);
                            if (job.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                            var limit = new ExtendedJobLimit();
                            // KILL_ON_JOB_CLOSE owns the direct worker; SILENT_BREAKAWAY_OK
                            // lets independently managed services survive worker completion.
                            limit.Basic.Flags = 0x2000U | (keepDescendants ? 0x1000U : 0U);
                            if (!SetInformationJobObject(job, 9, ref limit, Marshal.SizeOf(typeof(ExtendedJobLimit))) ||
                                !AssignProcessToJobObject(job, process.Process))
                                throw new Win32Exception(Marshal.GetLastWin32Error());
                        }
                        if (ResumeThread(process.Thread) == UInt32.MaxValue)
                            throw new Win32Exception(Marshal.GetLastWin32Error());
                        return new NoConsoleChild(process.Process, process.ProcessId, job);
                    }
                    catch
                    {
                        TerminateProcess(process.Process, 127);
                        CloseHandle(process.Process);
                        if (job != null) job.Dispose();
                        throw;
                    }
                    finally { CloseHandle(process.Thread); }
                }
                finally
                {
                    if (values != IntPtr.Zero) Marshal.FreeHGlobal(values);
                    if (initialized) DeleteProcThreadAttributeList(list);
                    if (list != IntPtr.Zero) Marshal.FreeHGlobal(list);
                }
            }
        }

        private static string RawArgumentsAfterPaths(string command, int count)
        {
            // The three prefix tokens are filesystem paths, which cannot contain
            // quotes. Leave the child's argument string completely unchanged.
            int index = 0;
            for (int token = 0; token < count; token++)
            {
                while (index < command.Length && Char.IsWhiteSpace(command[index])) index++;
                if (index < command.Length && command[index] == '"')
                {
                    index++;
                    while (index < command.Length && command[index] != '"') index++;
                    if (index < command.Length) index++;
                }
                else while (index < command.Length && !Char.IsWhiteSpace(command[index])) index++;
            }
            return command.Substring(index).TrimStart();
        }

        [STAThread]
        public static int Main(string[] args)
        {
            // no-console.exe [--tree] "log prefix" "absolute executable" original arguments
            bool ownTree = args.Length > 0 && args[0] == "--tree";
            int offset = ownTree ? 1 : 0;
            if (args.Length < offset + 2) return 64;
            try
            {
                using (var child = StartCore(args[offset + 1], RawArgumentsAfterPaths(Environment.CommandLine, offset + 3),
                    Environment.CurrentDirectory, args[offset] + ".stdout.log", args[offset] + ".stderr.log", true, !ownTree))
                {
                    child.WaitForExit();
                    return child.ExitCode;
                }
            }
            catch (Exception error)
            {
                try { File.AppendAllText(args[offset] + ".stderr.log", DateTime.Now.ToString("o") +
                    " launcher: " + error.Message + Environment.NewLine); }
                catch { }
                return 127;
            }
        }
    }
}
