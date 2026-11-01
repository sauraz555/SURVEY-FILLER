# Asks for your API keys (input hidden) and writes them to .env next to this script.
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
function Ask($label) {
    $s = Read-Host -AsSecureString $label
    (New-Object System.Net.NetworkCredential('', $s)).Password.Trim()
}
$typesafe = Ask "TypeSafe (Jev) API key - leave blank to skip"
$groq = Ask "Groq API key - leave blank to skip"
$lines = @()
$lines += if ($typesafe) { "LLM_PROVIDER=jev" } else { "LLM_PROVIDER=groq" }
if ($typesafe) { $lines += "TYPESAFE_API_KEY=$typesafe" }
if ($groq) { $lines += "GROQ_API_KEY=$groq" }
$lines | Set-Content -Encoding ascii (Join-Path $dir ".env")
Write-Host "Saved $(Join-Path $dir '.env') - go back to Claude and say it's done."
