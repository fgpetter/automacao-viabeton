"""Regressão da coleta usando DOM real, sem acesso ao SDM ou gravação de JSON."""
import unittest

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from baixar_anexos_imagens import coletar_propriedades


class FrameComEsperaCurta:
    """Mantém o DOM real e reduz apenas o prazo para cenários negativos."""
    name = 'cai_main'

    def __init__(self, frame):
        self.frame = frame

    def wait_for_selector(self, selector, **kwargs):
        kwargs['timeout'] = 400
        return self.frame.wait_for_selector(selector, **kwargs)

    def wait_for_function(self, expression, **kwargs):
        kwargs['timeout'] = 400
        return self.frame.wait_for_function(expression, **kwargs)

    def __getattr__(self, name):
        return getattr(self.frame, name)


class PopupDeTeste:
    def __init__(self, frame):
        self.frames = [FrameComEsperaCurta(frame)]


class ColetarPropriedadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content('<iframe name="cai_main"></iframe>')
        self.frame = self.page.frame(name='cai_main')
        self.addCleanup(self.page.close)

    def coletar(self, tabela):
        self.frame.set_content('<a id="tabHyprlnk1_1" href="#">PROPRIEDADES</a>' + tabela)
        return coletar_propriedades(PopupDeTeste(self.frame))

    def test_propriedades_visiveis(self):
        result = self.coletar('<table><tr><td>Endereço</td><td pdmqa="prop0">Rua A</td></tr></table>')
        self.assertEqual(result, {'propriedades': {'endereco': 'Rua A'}, 'observacoes': None})

    def test_propriedades_ocultas_com_texto_aninhado(self):
        result = self.coletar('''<div style="display:none"><table>
          <tr><td>Endereço</td><td pdmqa="prop0"><span style="display:none">Rua A</span></td></tr>
          <tr><td>Complemento</td><td pdmqa="prop1"></td></tr>
          </table><label for="obs">Observações</label><div id="obs"><span style="display:none">Texto</span></div></div>''')
        self.assertEqual(result, {'propriedades': {'endereco': 'Rua A', 'complemento': None}, 'observacoes': 'Texto'})

    def test_formulario_sem_indice_zero(self):
        result = self.coletar('<table><tr><td>Local</td><td pdmqa="prop1">Sala 2</td></tr></table>')
        self.assertEqual(result['propriedades'], {'local': 'Sala 2'})

    def test_preserva_quebras_de_linha_do_texto_visivel(self):
        result = self.coletar('<table><tr><td>Local</td><td pdmqa="prop0">Rua A<br>Sala 2</td></tr></table>')
        self.assertEqual(result['propriedades']['local'], 'Rua A\nSala 2')

    def test_ausencia_de_propriedades_continua_sendo_erro(self):
        with self.assertRaises(PlaywrightTimeout):
            self.coletar('<table><tr><td pdmqa="propaganda">Outro painel</td></tr></table>')

    def test_categoria_explicitamente_sem_propriedades_preserva_observacoes(self):
        result = self.coletar('''<table><tr><th>Nome</th><th>Valor</th><th>Exemplos</th></tr>
          <tr><td colspan="3">Não há propriedades definidas para este(a) categoria</td></tr></table>
          <label for="obs">Observações</label><div id="obs">Verificar no local</div>''')
        self.assertEqual(result, {'propriedades': {}, 'observacoes': 'Verificar no local'})

    def test_mensagem_de_outro_contexto_nao_aceita_categoria_vazia(self):
        with self.assertRaises(PlaywrightTimeout):
            self.coletar('<div>Não há propriedades definidas para este(a) categoria</div>')

    def test_chave_duplicada_continua_sendo_erro(self):
        with self.assertRaises(ValueError):
            self.coletar('<table><tr><td>Local</td><td pdmqa="prop0">A</td></tr><tr><td>Local</td><td pdmqa="prop1">B</td></tr></table>')


if __name__ == '__main__':
    unittest.main()
