# 把 EasyEDA Pro 主窗口切前台(后台窗口会让画布计算爬行/挂起)
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File runs/fg-easyeda.ps1
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);' -Name U -Namespace W
$p = Get-Process lceda-pro -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($p) {
  [W.U]::ShowWindow($p.MainWindowHandle, 9) | Out-Null
  [W.U]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
  Write-Output ("fg ok: " + $p.MainWindowTitle)
} else {
  Write-Output "no lceda-pro window"
}
