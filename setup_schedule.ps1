# Настройка автопостинга по расписанию через Планировщик заданий Windows.
# Создаёт две задачи: RecipesBot (рецепты, ежедневно) и RecipesPoll (опрос, еженедельно).
# Запуск (один раз):
#   powershell -ExecutionPolicy Bypass -File "C:\ИИ тг канал\setup_schedule.ps1"
#
# Чтобы изменить расписание — поправьте $Times / $PollDay / $PollTime и запустите заново.
# Чтобы удалить задачи:
#   Unregister-ScheduledTask -TaskName RecipesBot,RecipesPoll -Confirm:$false

$ErrorActionPreference = "Stop"

# ── Настройки ────────────────────────────────────────────────────────────────
$ScriptDir = "C:\ИИ тг канал"
$Times     = @("09:00", "14:00", "19:00")   # ← времена постов (3/день). Добавьте строки для 4–5/день.
$PollDay   = "Sunday"                        # день опроса-вовлечения
$PollTime  = "12:00"                         # время опроса
# ─────────────────────────────────────────────────────────────────────────────

$python = (Get-Command python).Source
if (-not $python) { throw "Python не найден в PATH. Установите Python или добавьте его в PATH." }

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)
$settings.DisallowStartIfOnBatteries = $false   # запускать задачи и при работе от батареи

# Задача 1 — рецепты (ежедневно)
$actionPost = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$ScriptDir\generate_and_post.py`"" -WorkingDirectory $ScriptDir
$trigPost = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
Register-ScheduledTask -TaskName "RecipesBot" -Action $actionPost -Trigger $trigPost `
    -Settings $settings -Description "Авто-постинг рецептов в Telegram" -Force | Out-Null

# Задача 2 — опрос-вовлечение (раз в неделю)
$actionPoll = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$ScriptDir\generate_and_post.py`" --poll" -WorkingDirectory $ScriptDir
$trigPoll = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $PollDay -At $PollTime
Register-ScheduledTask -TaskName "RecipesPoll" -Action $actionPoll -Trigger $trigPoll `
    -Settings $settings -Description "Еженедельный опрос-вовлечение" -Force | Out-Null

Write-Host ""
Write-Host "Готово. Созданы задачи:" -ForegroundColor Green
Write-Host "  RecipesBot  - рецепты: $($Times -join ', ') (ежедневно)"
Write-Host "  RecipesPoll - опрос:   $PollDay $PollTime (еженедельно)"
Write-Host ""
Write-Host "Проверить:       Get-ScheduledTask RecipesBot, RecipesPoll"
Write-Host "Запустить пост:   Start-ScheduledTask -TaskName RecipesBot"
Write-Host "Запустить опрос:  Start-ScheduledTask -TaskName RecipesPoll"
Write-Host "Удалить: Unregister-ScheduledTask -TaskName RecipesBot,RecipesPoll -Confirm:`$false"
