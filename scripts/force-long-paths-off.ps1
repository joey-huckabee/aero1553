# Force Windows long-path support OFF for this runner, and prove it took.
#
# Rust adds the \\?\ extended-length prefix itself, so it reaches a path over
# 260 characters whatever this switch says; Python's own file calls do not.
# The long-path tests (python/tests/test_long_paths.py,
# tests/conformance/long_paths.py) mean something only with the switch OFF,
# which is how a site machine may well be set, while a CI image may have it on.
# Inheriting the runner's setting would let them pass without testing anything,
# so this sets it, and then checks from a NEW process that an ordinary-spelled
# long path is really refused. If it is not, this fails the job.

$key = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
$before = (Get-ItemProperty -Path $key -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
Write-Host "LongPathsEnabled on this runner: $before"
Set-ItemProperty -Path $key -Name LongPathsEnabled -Value 0 -Type DWord
Write-Host "LongPathsEnabled now: $((Get-ItemProperty -Path $key).LongPathsEnabled)"

$probe = @'
import os, sys, tempfile
base = tempfile.mkdtemp()
deep = base
while len(deep) < 320:
    deep = os.path.join(deep, "d" * 60)
os.makedirs("\\\\?\\" + deep)
print("ordinary-spelled long path visible:", os.path.isdir(deep))
sys.exit(1 if os.path.isdir(deep) else 0)
'@
python -c $probe
if ($LASTEXITCODE -ne 0) {
    Write-Error "Long paths still work from a new process; the switch did not take effect, so the long-path tests would prove nothing."
    exit 1
}
