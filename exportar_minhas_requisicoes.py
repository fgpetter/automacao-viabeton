"""
Exporta "Requisições do grupo" do SDM e salva downloads/requisicoes_grupo.xls(x).

Caminho na UI:
  1. Gerenciador de filas → Requisições (expande a pasta se estiver fechada)
  2. Requisições do grupo
  3. Exportar($)

Credenciais (não coloque no código):
  set SDM_USER=seu_usuario
  set SDM_PASS=sua_senha

Uso:
  python exportar_minhas_requisicoes.py --headless
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import Frame, Page, sync_playwright, TimeoutError as PlaywrightTimeout

URL = "https://sd.banrisul.com.br:8443/CAisd/pdmweb5.exe"
STATE_FILE = Path(__file__).with_name("storage_state.json")
DOWNLOAD_DIR = Path(__file__).with_name("downloads")
EXPORT_STEM = "requisicoes_grupo"


def find_frame(page: Page, name: str, timeout_ms: int = 30000) -> Frame:
    elapsed = 0
    step = 250
    while elapsed < timeout_ms:
        for frame in page.frames:
            if frame.name == name:
                return frame
        page.wait_for_timeout(step)
        elapsed += step
    raise RuntimeError(f"Frame '{name}' não encontrado em {timeout_ms}ms")


def wait_for_list(page: Page, timeout_ms: int = 30000) -> Frame:
    elapsed = 0
    step = 400
    while elapsed < timeout_ms:
        for frame in page.frames:
            try:
                if (
                    frame.get_by_role("heading", name="Lista de Requisições", exact=True).count() > 0
                    and frame.locator("#imgBtn4, a:has-text('Exportar($)')").count() > 0
                ):
                    return frame
            except Exception:
                pass
        page.wait_for_timeout(step)
        elapsed += step
    raise RuntimeError("Lista de Requisições com botão Exportar($) não apareceu")


def login_se_preciso(page: Page, user: str | None, password: str | None) -> None:
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)

    # Já autenticado (frameset do SDM)
    if any(f.name == "product" for f in page.frames):
        print("Sessão já autenticada.")
        return

    # Tela de logon
    if page.locator("#USERNAME").count() == 0:
        raise RuntimeError(f"Nem logon nem SDM. Título: {page.title()} URL: {page.url}")

    if not user or not password:
        raise RuntimeError(
            "Tela de logon detectada. Defina SDM_USER e SDM_PASS e rode de novo."
        )

    print(f"Efetuando logon como {user}...")
    page.fill("#USERNAME", user)
    page.fill("#PIN", password)
    # HardcodedSub existe no DOM mas fica oculto; o botão visível é #imgBtn0 (Logon)
    page.locator("a#imgBtn0.loginbtn, a#imgBtn0").first.click(timeout=15000)

    # Aguarda frameset pós-login (ou mensagem de erro)
    try:
        page.wait_for_function(
            "() => Array.from(window.frames).some(f => { try { return f.name === 'product'; } catch(e) { return false } })",
            timeout=60000,
        )
    except PlaywrightTimeout:
        err = page.locator(".error, .loginerror, #error, font[color=red]").first
        msg = err.inner_text(timeout=2000) if err.count() else page.inner_text("body")[:300]
        raise RuntimeError(f"Logon não concluiu. Página: {page.title()} | {msg}")
    print("Logon OK.")


def abrir_requisicoes_do_grupo(scoreboard: Frame) -> dict:
    """Expande 'Requisições' se preciso e clica em 'Requisições do grupo'."""
    return scoreboard.evaluate(
        """() => {
          const txt = (el) => (el.innerText || '').replace(/\\s+/g, ' ').trim();
          const visivel = (el) => {
            if (!el) return false;
            const cs = window.getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') return false;
            return el.getBoundingClientRect().height > 0;
          };
          const botoes = () => Array.from(document.querySelectorAll('button, a'));
          const pasta = botoes().find(b => /^Requisições$/i.test(txt(b)))
            || document.getElementById('s5pm');
          const acharGrupo = () =>
            botoes().find(b => {
              const t = txt(b);
              return /^Requisições do grupo/i.test(t)
                && !/não atribu/i.test(t)
                && !/em atendimento/i.test(t);
            })
            || document.getElementById('s8ds');
          let grupo = acharGrupo();
          if (pasta && !visivel(grupo)) {
            pasta.click();
            grupo = acharGrupo();
          }
          if (!grupo) return { ok: false, reason: 'Requisicoes do grupo nao encontrado' };
          grupo.click();
          return { ok: true, id: grupo.id, text: txt(grupo) };
        }"""
    )


def exportar(page: Page, download_dir: Path) -> Path:
    scoreboard = find_frame(page, "scoreboard", timeout_ms=60000)
    # Aguarda itens da árvore (frame aparece antes do HTML do scoreboard)
    scoreboard.wait_for_selector("#s5pm, #s8ds", timeout=30000)
    page.wait_for_timeout(500)
    print("Abrindo 'Requisições' → 'Requisições do grupo'...")
    clicked = abrir_requisicoes_do_grupo(scoreboard)
    print(f"Scoreboard click: {clicked}")
    if not clicked.get("ok"):
        raise RuntimeError(f"Falha ao abrir Requisições do grupo: {clicked}")

    lista = wait_for_list(page, timeout_ms=45000)
    print("Lista pronta. Clicando em 'Exportar($)'...")

    downloads: list = []

    def on_download(d):
        print(f"Download iniciado: {d.suggested_filename}")
        downloads.append(d)

    page.on("download", on_download)

    def on_page(p):
        p.on("download", on_download)

    page.context.on("page", on_page)

    with page.expect_popup(timeout=30000) as popup_info:
        lista.evaluate(
            """() => {
              const btn = document.getElementById('imgBtn4')
                || Array.from(document.querySelectorAll('a,button'))
                     .find(el => /Exportar\\s*\\(\\$\\)/i.test(el.innerText || ''));
              if (!btn) throw new Error('Exportar($) nao encontrado');
              if (typeof ImgBtnExecute === 'function') {
                try { return ImgBtnExecute(4); } catch (e) {}
              }
              btn.click();
              return 'clicked';
            }"""
        )
    popup = popup_info.value
    print(f"Popup: {popup.url}")
    popup.on("download", on_download)

    # Aguarda o XLS (popup processa e dispara o download)
    deadline = 180_000
    step = 500
    elapsed = 0
    while not downloads and elapsed < deadline:
        page.wait_for_timeout(step)
        elapsed += step
        if elapsed % 10000 == 0:
            try:
                txt = popup.inner_text("body").replace("\n", " ").strip()[:120]
                print(f"  popup ({elapsed/1000:.0f}s): {txt}")
            except Exception:
                pass

    if not downloads:
        raise RuntimeError("Timeout aguardando download do XLS")

    download = downloads[0]
    # Nome próprio, preservando o formato XLS/XLSX fornecido pelo SDM.
    suffix = Path(download.suggested_filename or "export.xls").suffix
    target = download_dir / f"{EXPORT_STEM}{suffix or '.xls'}"
    download.save_as(target)
    print(f"Download salvo em: {target} ({target.stat().st_size} bytes)")
    try:
        popup.close()
    except Exception:
        pass
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--salvar-sessao", action="store_true", help="Salva storage_state.json após login")
    args = parser.parse_args()

    user = os.environ.get("SDM_USER")
    password = os.environ.get("SDM_PASS")
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context_kwargs = {
            "ignore_https_errors": True,
            "accept_downloads": True,
        }
        if STATE_FILE.exists() and not user:
            context_kwargs["storage_state"] = str(STATE_FILE)
            print(f"Usando sessão: {STATE_FILE}")
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        try:
            login_se_preciso(page, user, password)
            if args.salvar_sessao:
                context.storage_state(path=str(STATE_FILE))
                print(f"Sessão salva em {STATE_FILE}")

            path = exportar(page, DOWNLOAD_DIR)
            print(f"OK: {path}")
            return 0
        except PlaywrightTimeout as e:
            print(f"Timeout: {e}", file=sys.stderr)
            page.screenshot(path=str(DOWNLOAD_DIR / "erro_requisicoes.png"), full_page=True)
            return 1
        except Exception as e:
            print(f"Erro: {e}", file=sys.stderr)
            try:
                page.screenshot(path=str(DOWNLOAD_DIR / "erro_requisicoes.png"), full_page=True)
            except Exception:
                pass
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
