# 复刻 run3b r2 的 apply 前驱序列:六页 clear+read → 立即 block-apply --doc P1
$W = "0b6851eb-2831-469a-bc1b-6f1c8c45c67a"
foreach ($p in @("P1","P2","P3","P4","P5","P6")) {
    easyeda sch clear --doc $p --window $W *> $null
    easyeda sch read --page $p --window $W *> $null
    Write-Output "cleared+read $p"
}
Write-Output "---- apply now (foreground last on P6) ----"
easyeda sch block-apply block.usbc_ufp_power_or --instance repro2_usbc --spacing 300 --at 150,380 --bind 5V_BUS=5V --bind USB_DP=USB_DP --bind USB_DM=USB_DM --bind GND=GND --json --doc P1 --window $W 2>&1 | Select-String -Pattern "layout ","origin:","^placed","`"ok`"" | ForEach-Object { $_.Line }
