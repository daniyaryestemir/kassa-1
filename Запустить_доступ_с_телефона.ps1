#requires -Version 5.1
<#!
Окно для временного доступа к этому Flask-приложению через интернет.
Первый запуск скачает cloudflared.exe с официального CDN Cloudflare
в папку .tools проекта. Никаких портов в роутере открывать не нужно.
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    [System.Windows.Forms.MessageBox]::Show('Python не найден. Установите Python 3 и повторите запуск.', 'Нет Python', 'OK', 'Error')
    exit 1
}

$toolDir = Join-Path $projectRoot '.tools'
$cloudflared = Join-Path $toolDir 'cloudflared.exe'
$serverProcess = $null
$tunnelProcess = $null
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 500

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Доступ к сайту с телефона'
$form.Size = New-Object System.Drawing.Size(700, 390)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false

$title = New-Object System.Windows.Forms.Label
$title.Text = 'Временный доступ к сайту через интернет'
$title.Font = New-Object System.Drawing.Font('Segoe UI', 14, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(20, 18)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = 'Нажмите «Запустить», дождитесь ссылки и откройте её на телефоне. Компьютер должен оставаться включённым.'
$hint.AutoSize = $true
$hint.MaximumSize = New-Object System.Drawing.Size(650, 40)
$hint.Location = New-Object System.Drawing.Point(22, 55)

$start = New-Object System.Windows.Forms.Button
$start.Text = 'Запустить'
$start.Size = New-Object System.Drawing.Size(130, 36)
$start.Location = New-Object System.Drawing.Point(20, 100)

$stop = New-Object System.Windows.Forms.Button
$stop.Text = 'Остановить'
$stop.Size = New-Object System.Drawing.Size(130, 36)
$stop.Location = New-Object System.Drawing.Point(160, 100)
$stop.Enabled = $false

$copy = New-Object System.Windows.Forms.Button
$copy.Text = 'Копировать ссылку'
$copy.Size = New-Object System.Drawing.Size(150, 36)
$copy.Location = New-Object System.Drawing.Point(300, 100)
$copy.Enabled = $false

$status = New-Object System.Windows.Forms.Label
$status.Text = 'Статус: остановлен'
$status.AutoSize = $true
$status.Location = New-Object System.Drawing.Point(22, 154)

$urlBox = New-Object System.Windows.Forms.TextBox
$urlBox.ReadOnly = $true
$urlBox.Font = New-Object System.Drawing.Font('Consolas', 10)
$urlBox.Size = New-Object System.Drawing.Size(640, 28)
$urlBox.Location = New-Object System.Drawing.Point(20, 184)

$warning = New-Object System.Windows.Forms.Label
$warning.Text = 'Не передавайте ссылку посторонним: по ней будет доступно ваше приложение.'
$warning.ForeColor = [System.Drawing.Color]::DarkRed
$warning.AutoSize = $true
$warning.Location = New-Object System.Drawing.Point(22, 222)

$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = 'Vertical'
$log.Font = New-Object System.Drawing.Font('Consolas', 9)
$log.Size = New-Object System.Drawing.Size(640, 85)
$log.Location = New-Object System.Drawing.Point(20, 250)

$form.Controls.AddRange(@($title, $hint, $start, $stop, $copy, $status, $urlBox, $warning, $log))

function Write-Status([string]$text) {
    $status.Text = "Статус: $text"
    $log.AppendText("[$(Get-Date -Format 'HH:mm:ss')] $text`r`n")
}

function Stop-Access {
    $timer.Stop()
    foreach ($proc in @($tunnelProcess, $serverProcess)) {
        if ($proc -and -not $proc.HasExited) {
            try { $proc.Kill(); $proc.WaitForExit(3000) } catch { }
        }
    }
    $script:tunnelProcess = $null
    $script:serverProcess = $null
    $start.Enabled = $true
    $stop.Enabled = $false
    $copy.Enabled = $false
    $urlBox.Text = ''
    Write-Status 'остановлен'
}

$start.Add_Click({
    try {
        $start.Enabled = $false
        $stop.Enabled = $true
        $urlBox.Text = ''
        Write-Status 'запускаю сервер на http://127.0.0.1:5001'

        if (-not (Test-Path $cloudflared)) {
            New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
            Write-Status 'скачиваю Cloudflare Tunnel (только при первом запуске)'
            Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cloudflared
        }

        $serverInfo = New-Object System.Diagnostics.ProcessStartInfo
        $serverInfo.FileName = $python.Source
        $serverInfo.Arguments = 'run_lan_temp.py'
        $serverInfo.WorkingDirectory = $projectRoot
        $serverInfo.UseShellExecute = $false
        $serverInfo.CreateNoWindow = $true
        $script:serverProcess = [System.Diagnostics.Process]::Start($serverInfo)

        Start-Sleep -Milliseconds 700
        if ($serverProcess.HasExited) { throw 'Flask-сервер не запустился. Проверьте настройки базы данных и Python.' }

        $tunnelInfo = New-Object System.Diagnostics.ProcessStartInfo
        $tunnelInfo.FileName = $cloudflared
        $tunnelInfo.Arguments = 'tunnel --url http://127.0.0.1:5001 --no-autoupdate'
        $tunnelInfo.WorkingDirectory = $projectRoot
        $tunnelInfo.UseShellExecute = $false
        $tunnelInfo.CreateNoWindow = $true
        $tunnelInfo.RedirectStandardError = $true
        $script:tunnelProcess = [System.Diagnostics.Process]::Start($tunnelInfo)
        Write-Status 'создаю защищённую ссылку…'
        $timer.Start()
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Не удалось запустить', 'OK', 'Error')
        Stop-Access
    }
})

$timer.Add_Tick({
    if (-not $tunnelProcess -or $tunnelProcess.HasExited) {
        Write-Status 'туннель завершился'
        $timer.Stop()
        return
    }
    while (-not $tunnelProcess.StandardError.EndOfStream) {
        $line = $tunnelProcess.StandardError.ReadLine()
        $log.AppendText("$line`r`n")
        if ($line -match 'https://[-a-z0-9]+\.trycloudflare\.com') {
            $urlBox.Text = $matches[0]
            $copy.Enabled = $true
            Write-Status 'готово — откройте ссылку на телефоне'
            $timer.Stop()
            break
        }
    }
})

$copy.Add_Click({
    if ($urlBox.Text) {
        [System.Windows.Forms.Clipboard]::SetText($urlBox.Text)
        Write-Status 'ссылка скопирована'
    }
})
$stop.Add_Click({ Stop-Access })
$form.Add_FormClosing({ Stop-Access })

[void]$form.ShowDialog()
