<?php
/**
 * Ziel der Redirect-URL (EB_REDIRECT_URL) für die Enable-Banking-Autorisierung.
 *
 * Zeigt die vollständige aufgerufene URL inklusive ?code=... zum Kopieren an.
 * Die URL anschließend bei "enable-banking authorize" einfügen.
 * Die Seite speichert nichts; das Image (siehe Dockerfile) protokolliert Zugriffe ohne Parameter.
 */

declare(strict_types=1);

// Der Code ist ein einmaliges Geheimnis: nicht cachen, nicht indexieren, nicht weiterreichen.
header('Cache-Control: no-store');
header('Referrer-Policy: no-referrer');
header('X-Robots-Tag: noindex, nofollow');
header('X-Content-Type-Options: nosniff');
header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'");

$https = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
    || strtolower($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https';
$url = ($https ? 'https' : 'http') . '://' . ($_SERVER['HTTP_HOST'] ?? 'localhost')
    . ($_SERVER['REQUEST_URI'] ?? '/');

$code = $_GET['code'] ?? null;
$error = $_GET['error'] ?? null;
// In einfachen Anführungszeichen, damit $(...) oder `...` aus einer präparierten URL beim
// Einfügen ins Terminal nicht ausgeführt werden.
$command = "docker compose exec enable-banking enable-banking authorize --code '"
    . str_replace("'", "'\\''", $url) . "'";

function e(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}
?>
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bankfreigabe</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 48rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
  textarea { width: 100%; box-sizing: border-box; font: .9rem ui-monospace, monospace; padding: .5rem;
             resize: vertical; word-break: break-all; }
  button { font: inherit; padding: .4rem 1.2rem; margin-top: .4rem; cursor: pointer; }
  .ok { color: #1a7f37; }
  .error { color: #b42318; }
  .copied { margin-left: .6rem; color: #1a7f37; visibility: hidden; }
  .hint { color: #666; font-size: .9rem; }
</style>
</head>
<body>
<h1>Bankfreigabe</h1>

<?php if (is_string($error)): ?>
  <p class="error"><strong>Die Bank hat die Freigabe nicht erteilt:</strong>
    <?= e($error) ?> <?= e((string)($_GET['error_description'] ?? '')) ?></p>
<?php elseif (is_string($code) && $code !== ''): ?>
  <p class="ok">Freigabe erteilt. Die folgende URL kopieren und bei <code>enable-banking authorize</code> einfügen.</p>
<?php else: ?>
  <p class="error">Die URL enthält keinen Parameter <code>code</code>.</p>
<?php endif; ?>

<h2>Vollständige URL</h2>
<textarea id="url" rows="4" readonly><?= e($url) ?></textarea>
<button type="button" data-copy="url">URL kopieren</button><span class="copied" id="url-copied">Kopiert</span>

<?php if (is_string($code) && $code !== ''): ?>
<h2>Alternativ: fertiger Befehl</h2>
<p class="hint">Falls die Eingabeaufforderung von <code>authorize</code> bereits geschlossen ist.</p>
<textarea id="command" rows="4" readonly><?= e($command) ?></textarea>
<button type="button" data-copy="command">Befehl kopieren</button><span class="copied" id="command-copied">Kopiert</span>
<?php endif; ?>

<p class="hint">Der Code ist nur kurz gültig und kann nur einmal verwendet werden. Diese Seite speichert nichts.</p>

<script>
  document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var id = button.dataset.copy;
      var field = document.getElementById(id);
      var done = function () {
        var note = document.getElementById(id + '-copied');
        note.style.visibility = 'visible';
        setTimeout(function () { note.style.visibility = 'hidden'; }, 2000);
      };
      field.select();
      // Die Clipboard-API gibt es nur über HTTPS; sonst auf execCommand ausweichen.
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(field.value).then(done);
      } else if (document.execCommand('copy')) {
        done();
      }
    });
  });
  document.querySelectorAll('textarea').forEach(function (field) {
    field.addEventListener('focus', function () { field.select(); });
  });
</script>
</body>
</html>
