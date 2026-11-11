"""
Percorre as ocorrências da lista "Ocorrências do grupo", abre cada popup,
salva PROPRIEDADES em /var/www/sistema_viabeton/temp/ocorrencias.json,
depois vai em ANEXOS e baixa imagens (jpg/jpeg/png/jfif).
Arquivos .jfif são salvos como .jpg.

Logs: /var/www/sistema_viabeton/temp/logs/baixar_anexos_imagens.log
Console e arquivo com rotação de 10 MiB e 5 backups; use --log-file para mudar.
Horários seguem o fuso do servidor; execucao identifica cada rodada.
Exemplo: python baixar_anexos_imagens_logs.py --headless
Monitorar: tail -F /var/www/sistema_viabeton/temp/logs/baixar_anexos_imagens.log

Saída (preferencial):
  /var/www/sistema_viabeton/temp/images/<numero>/imagem1.jpg
Fallback (se o path preferencial não existir):
  /home/filipe/projects/sistema-via-beton/<numero>/imagem1.jpg

Credenciais (SDM_USER / SDM_PASS), nesta ordem (primeira definição vence):
  1. variáveis de ambiente já definidas
  2. .env ao lado deste script
  3. /var/www/sistema_viabeton/.env
  4. /home/filipe/projects/sistema-via-beton/.env
  5. storage_state.json (sessão salva), se existir e não houver user/pass

Uso:
  python baixar_anexos_imagens_logs.py --headless
  python baixar_anexos_imagens_logs.py --limite 3
  python baixar_anexos_imagens_logs.py --salvar-sessao

Já verificada = número presente em ocorrencias.json (pula sem abrir popup).
Para reprocessar, remova a entrada correspondente do JSON.
"""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar
from functools import wraps
from time import monotonic
from uuid import uuid4
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

from playwright.sync_api import Frame, Page, Download, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

URL = "https://sd.banrisul.com.br:8443/CAisd/pdmweb5.exe"
STATE_FILE = Path(__file__).with_name("storage_state.json")
OCORRENCIAS_FILE = Path("/var/www/sistema_viabeton/temp/ocorrencias.json")
IMAGES_DIR_PRIMARY = Path("/var/www/sistema_viabeton/temp/images")
IMAGES_DIR_FALLBACK = Path("/home/filipe/projects/sistema-via-beton")
ENV_CANDIDATES = (
    Path(__file__).with_name(".env"),
    Path("/var/www/sistema_viabeton/.env"),
    Path("/home/filipe/projects/sistema-via-beton/.env"),
)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".jfif"}



LOG = logging.getLogger("sdm")
ETAPA = ContextVar("etapa", default="execucao")
NUMERO = ContextVar("ocorrencia", default="-")
EXECUCAO = uuid4().hex[:12]
LOG_FILE = Path("/var/www/sistema_viabeton/temp/logs/baixar_anexos_imagens.log")


class FormatoSeguro(logging.Formatter):
    def format(self, record):
        record.etapa = ETAPA.get()
        record.ocorrencia = NUMERO.get()
        record.execucao = EXECUCAO
        texto = super().format(record)
        for chave in ("SDM_USER", "SDM_PASS"):
            segredo = os.environ.get(chave)
            if segredo:
                texto = texto.replace(segredo, "[OCULTO]")
        texto = re.sub(r"(?i)(SID[=:% ]+)[^+&\s]+", r"\1[OCULTO]", texto)
        return texto


def configurar_logs(arquivo):
    arquivo = Path(arquivo)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    formato = FormatoSeguro(
        "%(asctime)s %(levelname)s execucao=%(execucao)s pid=%(process)d "
        "ocorrencia=%(ocorrencia)s etapa=%(etapa)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    arquivo_handler = RotatingFileHandler(
        arquivo, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    console = logging.StreamHandler(sys.stdout)
    for antigo in LOG.handlers[:]:
        antigo.close()
        LOG.removeHandler(antigo)
    for handler in (arquivo_handler, console):
        handler.setFormatter(formato)
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    LOG.info("INICIO monitoramento arquivo=%s", arquivo)


def print(*args, sep=" ", end="\n", file=None, flush=False):
    """Encaminha as mensagens existentes para console e arquivo de log."""
    mensagem = sep.join(str(arg) for arg in args).strip()
    if not mensagem:
        return
    nivel = logging.INFO
    if re.search(r"(?i)(erro|falha|timeout)", mensagem):
        nivel = logging.WARNING if "alternativas" in mensagem else logging.ERROR
    LOG.log(nivel, mensagem)


def etapa(nome, ocorrencia=False):
    def decorar(func):
        @wraps(func)
        def executar(*args, **kwargs):
            token = ETAPA.set(nome)
            numero_token = None
            if ocorrencia:
                numero_token = NUMERO.set(str(kwargs.get("numero", args[2] if len(args) > 2 else "-")))
            inicio = monotonic()
            LOG.info("INICIO")
            try:
                resultado = func(*args, **kwargs)
                erro = resultado.get("erro") if isinstance(resultado, dict) else None
                if isinstance(resultado, dict) and resultado.get("ok") is False:
                    erro = resultado.get("reason", "etapa recusada")
                if nome == "download_imagens":
                    docs = kwargs.get("docs", args[2] if len(args) > 2 else [])
                    if len(resultado) != len(docs):
                        erro = f"Download incompleto: {len(resultado)}/{len(docs)} imagens"
                if erro:
                    LOG.error("ERRO duracao=%.3fs detalhe=%s", monotonic() - inicio, erro)
                else:
                    pulada = nome == "salvar_json" and resultado is False
                    if isinstance(resultado, dict):
                        pulada = pulada or resultado.get("pulada", False)
                    status = "PULADA" if pulada else "SUCESSO"
                    LOG.info("%s duracao=%.3fs", status, monotonic() - inicio)
                return resultado
            except Exception:
                LOG.exception("ERRO duracao=%.3fs", monotonic() - inicio)
                raise
            finally:
                if numero_token is not None:
                    NUMERO.reset(numero_token)
                ETAPA.reset(token)
        return executar
    return decorar


def load_dotenv(path: Path) -> bool:
    """Carrega KEY=VALUE do .env sem sobrescrever variáveis já definidas."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ[key] = value
    return True


@etapa("carregar_credenciais")
def load_credentials_env() -> list[Path]:
    """Carrega todos os .env candidatos (primeiro arquivo define cada chave)."""
    loaded: list[Path] = []
    for candidate in ENV_CANDIDATES:
        if load_dotenv(candidate):
            loaded.append(candidate)
    return loaded


@etapa("resolver_destino")
def resolve_images_dir() -> Path:
    """Destino das imagens: produção se existir; senão fallback do projeto."""
    if IMAGES_DIR_PRIMARY.is_dir():
        return IMAGES_DIR_PRIMARY
    return IMAGES_DIR_FALLBACK


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


@etapa("aguardar_lista")
def wait_for_list(page: Page, timeout_ms: int = 30000) -> Frame:
    elapsed = 0
    step = 400
    while elapsed < timeout_ms:
        for frame in page.frames:
            try:
                if frame.locator("#imgBtn4, a:has-text('Exportar($)')").count() > 0:
                    return frame
            except Exception:
                pass
        page.wait_for_timeout(step)
        elapsed += step
    raise RuntimeError("Lista com botão Exportar($) não apareceu")


@etapa("autenticacao")
def login_se_preciso(page: Page, user: str | None, password: str | None) -> None:
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)

    if any(f.name == "product" for f in page.frames):
        print("Sessão já autenticada.")
        return

    if page.locator("#USERNAME").count() == 0:
        raise RuntimeError(f"Nem logon nem SDM. Título: {page.title()} URL: {page.url}")

    if not user or not password:
        raise RuntimeError(
            "Tela de logon detectada. Defina SDM_USER e SDM_PASS e rode de novo."
        )

    print("Efetuando logon com credenciais configuradas...")
    page.fill("#USERNAME", user)
    page.fill("#PIN", password)
    page.locator("a#imgBtn0.loginbtn, a#imgBtn0").first.click(timeout=15000)

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


@etapa("abrir_fila")
def abrir_ocorrencias_do_grupo(scoreboard: Frame) -> dict:
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
          const pasta = botoes().find(b => /^Ocorrências$/i.test(txt(b)))
            || document.getElementById('s9pm');
          const acharGrupo = () =>
            botoes().find(b => {
              const t = txt(b);
              return /^Ocorrências do grupo/i.test(t)
                && !/não atribu/i.test(t)
                && !/em atendimento/i.test(t);
            })
            || document.getElementById('s13ds');
          let grupo = acharGrupo();
          if (pasta && !visivel(grupo)) {
            pasta.click();
            grupo = acharGrupo();
          }
          if (!grupo) return { ok: false, reason: 'Ocorrencias do grupo nao encontrado' };
          grupo.click();
          return { ok: true, id: grupo.id, text: txt(grupo) };
        }"""
    )


@etapa("listar_ocorrencias")
def listar_ocorrencias(lista: Frame) -> list[dict]:
    return lista.evaluate(
        """() => {
          const txt = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
          const seen = new Set();
          const out = [];
          for (const a of document.querySelectorAll('a[id^="rslnk_"]')) {
            const t = txt(a);
            if (!/^\\d{4,}$/.test(t) || seen.has(t)) continue;
            seen.add(t);
            out.push({ numero: t, id: a.id || '' });
          }
          return out;
        }"""
    )


@etapa("abrir_popup")
def clicar_numero_ocorrencia(lista: Frame, numero: str) -> None:
    lista.evaluate(
        """(numero) => {
          const txt = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
          const link = Array.from(document.querySelectorAll('a[id^="rslnk_"], a'))
            .find(el => txt(el) === numero);
          if (!link) throw new Error('Link da ocorrência sumiu: ' + numero);
          link.click();
        }""",
        numero,
    )


def wait_frame_name(page: Page, name: str, timeout_ms: int = 20000) -> Frame:
    return find_frame(page, name, timeout_ms=timeout_ms)


def clicar_texto(frame: Frame, padroes: list[str]) -> dict:
    """Clica no menor elemento que casa o padrão (evita TD que junta várias abas)."""
    return frame.evaluate(
        """(padroes) => {
          const txt = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
          // Preferir <a> (abas reais usam tabHyprlnk*)
          const els = Array.from(document.querySelectorAll('a, button, td, span, li, div, label'));
          for (const pat of padroes) {
            const re = new RegExp(pat, 'i');
            const matches = els
              .filter(e => {
                const t = txt(e);
                return t && t.length <= 40 && re.test(t);
              })
              .sort((a, b) => {
                const pa = a.tagName === 'A' ? 0 : 1;
                const pb = b.tagName === 'A' ? 0 : 1;
                if (pa !== pb) return pa - pb;
                return txt(a).length - txt(b).length;
              });
            if (matches.length) {
              const el = matches[0];
              el.click();
              return { ok: true, text: txt(el), id: el.id || '', tag: el.tagName };
            }
          }
          return { ok: false, reason: 'texto nao encontrado', padroes };
        }""",
        padroes,
    )


def clicar_aba_por_id(frame: Frame, element_id: str) -> dict:
    return frame.evaluate(
        """(elementId) => {
          const el = document.getElementById(elementId);
          if (!el) return { ok: false, reason: 'id nao encontrado', id: elementId };
          el.click();
          return {
            ok: true,
            id: elementId,
            text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
            tag: el.tagName,
          };
        }""",
        element_id,
    )


@etapa("abrir_anexos")
def abrir_aba_anexos(popup: Page) -> Frame:
    """Ativa a sub-aba ANEXOS (a#tabHyprlnk1_2) e devolve o frame da lista.

    No popup, "1. Informações adicionais" já vem ativo; a sub-aba default é
    PROPRIEDADES (tabHyprlnk1_1). Clicar no TD pai de ANEXOS NÃO troca o conteúdo —
    é preciso clicar o link a#tabHyprlnk1_2.
    """
    mainf = wait_frame_name(popup, "cai_main", timeout_ms=30000)
    popup.wait_for_timeout(500)

    r2 = clicar_aba_por_id(mainf, "tabHyprlnk1_2")
    if not r2.get("ok"):
        r2 = clicar_texto(mainf, [r"^2\s*\.?\s*ANEXOS$", r"^ANEXOS$"])
    print(f"  Aba ANEXOS: {r2}")
    if not r2.get("ok"):
        raise RuntimeError(f"Não encontrei a aba ANEXOS: {r2}")

    # Aguarda "Lista de anexos" / iframe attmnt carregar
    deadline = 25000
    step = 400
    elapsed = 0
    while elapsed < deadline:
        for fr in popup.frames:
            try:
                nome = fr.name or ""
                url = fr.url or ""
                if nome == "attmnt_iframe" and url and "empty.html" not in url and url != "about:blank":
                    hit = fr.evaluate(
                        """() => {
                          const t = (document.body && (document.body.innerText || '')) || '';
                          return t.length > 10;
                        }"""
                    )
                    if hit:
                        return fr
                hit = fr.evaluate(
                    """() => {
                      const t = (document.body && (document.body.innerText || '')) || '';
                      return /Lista de anexos/i.test(t);
                    }"""
                )
                if hit:
                    return fr
            except Exception:
                pass
        popup.wait_for_timeout(step)
        elapsed += step

    ainda_props = mainf.evaluate(
        """() => {
          const t = (document.body && document.body.innerText || '');
          return /Nome\\s+Valor\\s+Exemplos/i.test(t) && !/Lista de anexos/i.test(t);
        }"""
    )
    if ainda_props:
        raise RuntimeError(
            "Clique em ANEXOS não ativou a lista (ainda em Propriedades). "
            "Esperado a#tabHyprlnk1_2."
        )

    for fr in popup.frames:
        if fr.name == "attmnt_iframe":
            return fr
    return mainf


def listar_documentos(frame: Frame, so_imagens: bool = True) -> list[dict]:
    """Lista documentos na grade de anexos (links ou texto de célula)."""
    return frame.evaluate(
        """({ soImagens }) => {
          const txt = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
          const isImg = (name) => /\\.(jpe?g|png|jfif)$/i.test(name || '');
          const isDoc = (name) => /\\.[a-z0-9]{2,5}$/i.test(name || '');
          const out = [];
          const seen = new Set();

          const push = (nome, el) => {
            if (!nome) return;
            if (soImagens && !isImg(nome)) return;
            if (!soImagens && !isDoc(nome) && !isImg(nome)) return;
            const href = el ? (el.getAttribute('href') || '') : '';
            const id = el && el.id ? el.id : '';
            const onclick = el ? (el.getAttribute('onclick') || '').slice(0, 200) : '';
            const key = nome + '|' + href + '|' + id;
            if (seen.has(key)) return;
            seen.add(key);
            out.push({ nome, id, href, onclick, tag: el ? el.tagName : 'TD' });
          };

          // 1) Links com nome de arquivo
          for (const a of document.querySelectorAll('a')) {
            const name = txt(a) || a.getAttribute('title') || '';
            push(name, a);
          }

          // 2) Células da tabela cujo texto parece arquivo
          for (const td of document.querySelectorAll('td')) {
            const name = txt(td);
            if (!isDoc(name) && !isImg(name)) continue;
            // Preferir link dentro da célula, se houver
            const a = td.querySelector('a');
            push(name, a || td);
          }

          return out;
        }""",
        {"soImagens": so_imagens},
    )


def listar_documentos_imagem(frame: Frame) -> list[dict]:
    return listar_documentos(frame, so_imagens=True)


@etapa("listar_anexos")
def coletar_docs_em_frames(popup: Page, so_imagens: bool = True) -> tuple[list[dict], Frame | None]:
    """Varre todos os frames e devolve docs + frame onde foram encontrados."""
    melhor: list[dict] = []
    frame_hit: Frame | None = None
    for fr in popup.frames:
        try:
            docs = listar_documentos(fr, so_imagens=so_imagens)
        except Exception:
            continue
        if len(docs) > len(melhor):
            melhor = docs
            frame_hit = fr
    return melhor, frame_hit


def extensao_de(nome: str) -> str:
    """Extensão de saída; .jfif é normalizado para .jpg."""
    m = re.search(r"\.(jpe?g|png|jfif)$", nome, re.I)
    if not m:
        return ".jpg"
    ext = m.group(0).lower()
    if ext == ".jfif":
        return ".jpg"
    return ext


@etapa("salvar_imagem")
def salvar_download(download: Download, destino: Path) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(destino)
    return destino


@etapa("download_imagens")
def baixar_imagens_da_ocorrencia(
    popup: Page,
    frame_anexos: Frame,
    docs: list[dict],
    pasta: Path,
) -> list[Path]:
    salvos: list[Path] = []
    for i, doc in enumerate(docs, start=1):
        nome_orig = doc.get("nome") or f"imagem{i}"
        ext = extensao_de(nome_orig)
        destino = pasta / f"imagem{i}{ext}"
        print(f"    Baixando [{i}/{len(docs)}]: {nome_orig} → {destino.name}")

        seletor_js = """(doc) => {
          const txt = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
          let el = null;
          if (doc.id) el = document.getElementById(doc.id);
          if (!el) {
            el = Array.from(document.querySelectorAll('a')).find(a =>
              txt(a) === doc.nome || (a.getAttribute('title') || '') === doc.nome
            );
          }
          if (!el) {
            el = Array.from(document.querySelectorAll('td')).find(td => txt(td) === doc.nome);
            if (el) {
              const a = el.querySelector('a');
              if (a) el = a;
            }
          }
          if (!el) throw new Error('Link do documento nao encontrado: ' + doc.nome);
          el.click();
        }"""

        try:
            with popup.expect_download(timeout=30000) as dl_info:
                frame_anexos.evaluate(seletor_js, doc)
            download = dl_info.value
            path = salvar_download(download, destino)
            print(f"    OK download: {path} ({path.stat().st_size} bytes)")
            salvos.append(path)
            continue
        except PlaywrightTimeout:
            LOG.warning("Download direto excedeu o tempo; tentando alternativas arquivo=%s", destino.name)
        except Exception as e:
            print(f"    expect_download falhou ({e}); tentando alternativas...")

        # Alternativa 1: popup/nova página com a imagem
        try:
            with popup.expect_popup(timeout=5000) as pop_info:
                frame_anexos.evaluate(seletor_js, doc)
            img_page = pop_info.value
            img_page.wait_for_load_state("domcontentloaded", timeout=15000)
            url = img_page.url
            if url and url != "about:blank":
                resp = popup.context.request.get(url)
                if resp.ok:
                    destino.write_bytes(resp.body())
                    print(f"    OK via popup URL: {destino} ({destino.stat().st_size} bytes)")
                    salvos.append(destino)
                    img_page.close()
                    continue
            img_page.close()
        except Exception:
            LOG.warning("Alternativa popup falhou arquivo=%s", destino.name, exc_info=True)

        # Alternativa 2: href http(s) direto
        href = (doc.get("href") or "").strip()
        if href and not href.lower().startswith("javascript:"):
            try:
                abs_url = frame_anexos.evaluate(
                    "(h) => { try { return new URL(h, location.href).href } catch(e) { return h } }",
                    href,
                )
                resp = popup.context.request.get(abs_url)
                if resp.ok and len(resp.body()) > 0:
                    destino.write_bytes(resp.body())
                    print(f"    OK via href: {destino} ({destino.stat().st_size} bytes)")
                    salvos.append(destino)
                    continue
            except Exception as e:
                print(f"    href falhou: {e}")

        print(f"    FALHA ao baixar: {nome_orig}")
    return salvos


def pasta_ocorrencia(numero: str, images_dir: Path | None = None) -> Path:
    base = images_dir if images_dir is not None else resolve_images_dir()
    return base / numero


@etapa("ler_json")
def carregar_ocorrencias(arquivo: Path = OCORRENCIAS_FILE) -> dict:
    """Não sobrescreve silenciosamente um arquivo inválido."""
    if not arquivo.exists():
        return {"ocorrencias": {}}
    dados = json.loads(arquivo.read_text(encoding="utf-8"))
    if not isinstance(dados, dict) or not isinstance(dados.get("ocorrencias"), dict):
        raise ValueError(f"Estrutura inválida em {arquivo}: esperado objeto 'ocorrencias'")
    return dados


@etapa("salvar_json")
def salvar_propriedades(numero: str, registro: dict, arquivo: Path = OCORRENCIAS_FILE) -> bool:
    """Acumula uma ocorrência e substitui o JSON atomicamente após gravar."""
    dados = carregar_ocorrencias(arquivo)
    if numero in dados["ocorrencias"]:
        return False
    dados["ocorrencias"][numero] = registro
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=arquivo.parent,
            prefix=f".{arquivo.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporario = Path(stream.name)
            json.dump(dados, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporario, arquivo)
    finally:
        if temporario is not None:
            temporario.unlink(missing_ok=True)
    return True


@etapa("coletar_propriedades")
def coletar_propriedades(popup: Page) -> dict:
    mainf = wait_frame_name(popup, "cai_main", timeout_ms=30000)
    mainf.wait_for_selector("a#tabHyprlnk1_1", timeout=30000)
    aba = clicar_aba_por_id(mainf, "tabHyprlnk1_1")
    if not aba.get("ok"):
        raise RuntimeError(f"Não encontrei a aba PROPRIEDADES: {aba}")
    mainf.wait_for_selector('td[pdmqa="prop0"]', timeout=30000)
    extraidos = mainf.evaluate(
        """() => {
          const txt = el => (el?.innerText || '').trim();
          const campos = Array.from(document.querySelectorAll('td[pdmqa]'))
            .filter(el => /^prop\\d+$/.test(el.getAttribute('pdmqa')))
            .map(el => ({nome: txt(el.previousElementSibling), valor: txt(el)}));
          const label = Array.from(document.querySelectorAll('label[for]'))
            .find(el => txt(el) === 'Observações');
          const obs = label ? document.getElementById(label.htmlFor) : null;
          return {campos, observacoes: txt(obs) || null};
        }"""
    )
    propriedades = {}
    for campo in extraidos["campos"]:
        nome = unicodedata.normalize("NFKD", campo["nome"])
        nome = nome.encode("ascii", "ignore").decode().lower()
        chave = re.sub(r"[^a-z0-9]+", "_", nome).strip("_")
        if not chave or chave in propriedades:
            raise ValueError(f"Nome de propriedade inválido ou duplicado: {campo['nome']}")
        propriedades[chave] = campo["valor"] or None
    if not propriedades:
        raise RuntimeError("Nenhuma propriedade encontrada na ocorrência")
    return {"propriedades": propriedades, "observacoes": extraidos["observacoes"]}


@etapa("processar_ocorrencia", ocorrencia=True)
def processar_ocorrencia(
    page: Page, lista: Frame, numero: str, images_dir: Path
) -> dict:
    print(f"\n=== Ocorrência {numero} ===")
    resultado = {"numero": numero, "imagens": [], "docs_imagem": 0, "erro": None}
    if numero in carregar_ocorrencias()["ocorrencias"]:
        print("  Pulando: ocorrência já presente no JSON")
        resultado["pulada"] = True
        return resultado
    with page.expect_popup(timeout=30000) as popup_info:
        clicar_numero_ocorrencia(lista, numero)
    popup = popup_info.value
    popup.wait_for_load_state("domcontentloaded", timeout=30000)
    popup.wait_for_timeout(1500)

    try:
        registro = coletar_propriedades(popup)
        if not salvar_propriedades(numero, registro):
            print("  Pulando: ocorrência já presente no JSON")
            resultado["pulada"] = True
            return resultado
        print(f"  Propriedades salvas: {OCORRENCIAS_FILE}")
        frame_anexos = abrir_aba_anexos(popup)
        popup.wait_for_timeout(1500)

        docs, frame_hit = coletar_docs_em_frames(popup, so_imagens=True)
        if frame_hit is not None:
            frame_anexos = frame_hit

        resultado["docs_imagem"] = len(docs)
        print(f"  Imagens na lista: {len(docs)}")
        for d in docs:
            print(f"    - {d.get('nome')} ({d.get('tag')})")

        pasta = pasta_ocorrencia(numero, images_dir)
        pasta.mkdir(parents=True, exist_ok=True)

        if not docs:
            todos, _ = coletar_docs_em_frames(popup, so_imagens=False)
            print(f"  Nenhum jpg/png/jfif. Outros documentos: {len(todos)}")
            for d in todos:
                print(f"    · {d.get('nome')}")
            print(f"  Sem imagens; propriedades já registradas em {OCORRENCIAS_FILE}")
            return resultado

        salvos = baixar_imagens_da_ocorrencia(popup, frame_anexos, docs, pasta)
        resultado["imagens"] = [str(p) for p in salvos]
        if len(salvos) != len(docs):
            resultado["erro"] = f"Download incompleto: {len(salvos)}/{len(docs)} imagens"
    except Exception as e:
        resultado["erro"] = str(e)
        LOG.exception("ERRO no processamento da ocorrência")
        # O JSON já salvo é preservado mesmo se o download falhar.
        pasta = pasta_ocorrencia(numero, images_dir)
        if pasta.is_dir() and not any(pasta.iterdir()):
            try:
                pasta.rmdir()
            except Exception:
                pass
    finally:
        try:
            popup.close()
            LOG.info("SUCESSO fechamento_popup")
        except Exception:
            LOG.exception("ERRO fechamento_popup")
    return resultado


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baixa anexos de imagem das ocorrências do grupo no SDM"
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--salvar-sessao", action="store_true")
    parser.add_argument(
        "--limite",
        type=int,
        default=0,
        help="Processa no máximo N ocorrências (0 = todas da lista)",
    )
    parser.add_argument(
        "--numero",
        action="append",
        default=[],
        help="Processa só este número de ocorrência (pode repetir). Ex: --numero 9899362",
    )
    parser.add_argument("--log-file", type=Path, default=LOG_FILE,
                        help="Arquivo de log (rotação de 10 MiB, 5 backups)")
    args = parser.parse_args()
    configurar_logs(args.log_file)

    try:
        cadastradas = carregar_ocorrencias()["ocorrencias"]
    except (OSError, ValueError) as e:
        print(f"Erro ao ler {OCORRENCIAS_FILE}: {e}", file=sys.stderr)
        return 1

    env_loaded = load_credentials_env()
    user = os.environ.get("SDM_USER")
    password = os.environ.get("SDM_PASS")
    if env_loaded:
        print(f".env carregado: {', '.join(str(p) for p in env_loaded)}")
    if user:
        print("Credenciais SDM configuradas")
    else:
        print("SDM_USER não definido (usará storage_state se existir)")

    images_dir = resolve_images_dir()
    print(f"Destino das imagens: {images_dir}")
    if images_dir == IMAGES_DIR_FALLBACK and not IMAGES_DIR_PRIMARY.is_dir():
        print(f"  (fallback: {IMAGES_DIR_PRIMARY} não existe)")
    images_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        LOG.info("INICIO iniciar_navegador")
        browser = p.chromium.launch(headless=args.headless)
        LOG.info("SUCESSO iniciar_navegador")
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

            scoreboard = find_frame(page, "scoreboard", timeout_ms=60000)
            scoreboard.wait_for_selector("#s9pm, #s13ds", timeout=30000)
            page.wait_for_timeout(500)
            print("Abrindo 'Ocorrências' → 'Ocorrências do grupo'...")
            clicked = abrir_ocorrencias_do_grupo(scoreboard)
            print(f"Scoreboard click: {clicked}")
            if not clicked.get("ok"):
                raise RuntimeError(f"Falha ao abrir Ocorrências do grupo: {clicked}")

            lista = wait_for_list(page, timeout_ms=45000)
            ocorrencias = listar_ocorrencias(lista)
            if not ocorrencias:
                raise RuntimeError("Nenhuma ocorrência encontrada na lista")

            if args.numero:
                wanted = {str(n) for n in args.numero}
                ocorrencias = [o for o in ocorrencias if o["numero"] in wanted]
                if not ocorrencias:
                    raise RuntimeError(
                        f"Nenhum dos números {sorted(wanted)} está na lista atual"
                    )

            if args.limite and args.limite > 0:
                ocorrencias = ocorrencias[: args.limite]

            pendentes = []
            puladas = []
            for o in ocorrencias:
                if o["numero"] in cadastradas:
                    puladas.append(o["numero"])
                else:
                    pendentes.append(o)

            print(f"Ocorrências na seleção: {len(ocorrencias)}")
            print(f"  Já presentes no JSON: {len(puladas)}")
            print(f"  Pendentes: {len(pendentes)}")
            for n in puladas:
                print(f"  · pulando {n} (já presente no JSON)")
            for o in pendentes:
                print(f"  - processar {o['numero']}")

            resultados = []
            for o in pendentes:
                # Reobtém o frame da lista (pode invalidar após popups)
                lista = wait_for_list(page, timeout_ms=30000)
                resultados.append(
                    processar_ocorrencia(page, lista, o["numero"], images_dir)
                )

            print("\n========== RESUMO ==========")
            if puladas:
                print(f"  Puladas (já verificadas): {len(puladas)}")
            total_imgs = 0
            falhas = 0
            for r in resultados:
                n = len(r["imagens"])
                total_imgs += n
                status = "OK" if not r["erro"] else f"ERRO: {r['erro']}"
                if r["erro"]:
                    falhas += 1
                print(
                    f"  {r['numero']}: {n}/{r['docs_imagem']} imagens salvas — {status}"
                )
                for p in r["imagens"]:
                    print(f"    → {p}")

            print(f"\nTotal de imagens salvas: {total_imgs}")
            if falhas:
                print(f"Ocorrências com erro: {falhas}", file=sys.stderr)
                return 2
            return 0

        except PlaywrightTimeout as e:
            LOG.exception("ERRO timeout na execução")
            return 1
        except Exception as e:
            LOG.exception("ERRO na execução")
            return 1
        finally:
            browser.close()
            LOG.info("SUCESSO fechar_navegador")


if __name__ == "__main__":
    inicio = monotonic()
    try:
        codigo = main()
    except Exception:
        if LOG.handlers:
            LOG.exception("ERRO fatal na execução")
        else:
            logging.basicConfig(level=logging.ERROR)
            LOG.exception("Não foi possível iniciar o monitoramento")
        codigo = 1
    if LOG.handlers:
        LOG.log(logging.INFO if codigo == 0 else logging.ERROR,
                "FIM codigo_saida=%s duracao=%.3fs", codigo, monotonic() - inicio)
    logging.shutdown()
    raise SystemExit(codigo)
