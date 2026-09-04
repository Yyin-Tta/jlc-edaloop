# 校准A:复刻 run4 r1 的四块几何,逐页 clusters --strict 取错误目录
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force run\calib | Out-Null

function Apply-And-Cluster {
    param($page, $applyArgs)
    Write-Output "`n############ $page ############"
    & easyeda sch clear --doc $page | Out-Null
    $json = & easyeda @applyArgs 2>$null | Out-String
    [IO.File]::WriteAllText("e:\jlc-edaloop\run\calib\$page-apply.json", $json)
    $ok = 'apply-rc?'
    try { $m = $json | ConvertFrom-Json; $ok = "ok=$($m.ok) placed=$($m.placed.Count)" } catch { $ok = 'parse-fail' }
    Write-Output "APPLY $ok"
    & easyeda sch clusters --strict --doc $page 2>&1 | Select-Object -Last 40
}

Apply-And-Cluster 'P1' @('sch','block-apply','block.usbc_ufp_power_or','--instance','usbc_entry','--spacing','250','--at','100,300','--bind','5V_BUS=5V','--bind','USB_DP=USB_DP','--bind','USB_DM=USB_DM','--bind','GND=GND','--json','--doc','P1')
Apply-And-Cluster 'P2' @('sch','block-apply','block.sy8089_buck_3v3','--instance','ldo_3v3','--spacing','250','--at','100,300','--bind','VIN=5V','--bind','3V3=3V3','--bind','EN=5V','--bind','GND=GND','--json','--doc','P2')
Apply-And-Cluster 'P4' @('sch','block-apply','block.tactile_boot_reset','--instance','btn_boot_reset','--spacing','250','--at','100,610','--bind','IO0=BOOT0','--bind','EN=NRST','--bind','GND=GND','--json','--doc','P4')
Apply-And-Cluster 'P5' @('sch','block-apply','block.sp3485_rs485_halfduplex','--instance','rs485_xcvr','--spacing','250','--at','100,300','--bind','DI=MCU_TX','--bind','RO=MCU_RX','--bind','DE_RE=RS485_DE','--bind','A=RS485_A','--bind','B=RS485_B','--bind','3V3=3V3','--bind','GND=GND','--json','--doc','P5')
