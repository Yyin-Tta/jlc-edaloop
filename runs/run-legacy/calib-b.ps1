# 校准B:去掉 --instance(内部网走默认位号短名) + 实测包络定 at/spacing
$ErrorActionPreference = 'Continue'

function Apply-And-Cluster {
    param($page, $applyArgs)
    Write-Output "`n############ $page ############"
    & easyeda sch clear --doc $page | Out-Null
    $json = & easyeda @applyArgs 2>$null | Out-String
    [IO.File]::WriteAllText("e:\jlc-edaloop\run\calib\$page-apply.json", $json)
    try { $m = $json | ConvertFrom-Json; Write-Output "APPLY ok=$($m.ok) status=$($m.status) placed=$($m.placed.Count)" } catch { Write-Output "APPLY parse-fail" }
    & easyeda sch clusters --strict --doc $page 2>&1 | Where-Object { $_ -notmatch 'NativeCommandError|CategoryInfo|FullyQualifiedErrorId|^\s*\+|At E:' } | Select-Object -Last 32
}

Apply-And-Cluster 'P1' @('sch','block-apply','block.usbc_ufp_power_or','--spacing','250','--at','150,300','--bind','5V_BUS=5V','--bind','USB_DP=USB_DP','--bind','USB_DM=USB_DM','--bind','GND=GND','--json','--doc','P1')
Apply-And-Cluster 'P2' @('sch','block-apply','block.sy8089_buck_3v3','--spacing','250','--at','170,300','--bind','VIN=5V','--bind','3V3=3V3','--bind','EN=5V','--bind','GND=GND','--json','--doc','P2')
Apply-And-Cluster 'P4' @('sch','block-apply','block.tactile_boot_reset','--spacing','250','--at','150,600','--bind','IO0=BOOT0','--bind','EN=NRST','--bind','GND=GND','--json','--doc','P4')
Apply-And-Cluster 'P5' @('sch','block-apply','block.sp3485_rs485_halfduplex','--spacing','210','--at','336,300','--bind','DI=MCU_TX','--bind','RO=MCU_RX','--bind','DE_RE=RS485_DE','--bind','A=RS485_A','--bind','B=RS485_B','--bind','3V3=3V3','--bind','GND=GND','--json','--doc','P5')
