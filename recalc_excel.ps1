# Recalculate Delay_Cost_Model.xlsx in Excel (so cached values exist) and report formula errors.
param([string]$Path = "$PSScriptRoot\Delay_Cost_Model.xlsx")
$full = (Resolve-Path $Path).Path
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false; $xl.DisplayAlerts = $false
try {
    $wb = $xl.Workbooks.Open($full)
    $xl.CalculateFull()
    $errs = @(); $nform = 0
    foreach ($ws in $wb.Worksheets) {
        $used = $ws.UsedRange
        foreach ($c in $used.Cells) {
            if ($c.HasFormula) {
                $nform++
                if ($c.Text -match '^#(N/A|VALUE!|REF!|DIV/0!|NUM!|NAME\?|NULL!|SPILL!|CALC!)') { $errs += "$($ws.Name)!$($c.Address($false,$false)) $($c.Text) :: $($c.Formula)" }
            }
        }
    }
    $wb.Save(); $wb.Close($true)
    "formulas: $nform  errors: $($errs.Count)"
    $errs | Select-Object -First 30
} finally { $xl.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl) }
