# Forces GitHub to recalculate the odds and republish the site right now.
# Takes about a minute to show up on the page.
& "C:\Program Files\GitHub CLI\gh.exe" workflow run update-site.yml --repo thawes13/guillotine-watch
Write-Host "Requested. The site will show new numbers in about a minute."
