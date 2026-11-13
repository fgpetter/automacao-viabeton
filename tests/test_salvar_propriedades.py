import grp
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from baixar_anexos_imagens import carregar_ocorrencias, salvar_propriedades


class SalvarPropriedadesTest(unittest.TestCase):
    def test_permissoes_aplicadas_em_cada_substituicao(self):
        with tempfile.TemporaryDirectory() as directory:
            arquivo = Path(directory) / 'ocorrencias.json'
            # O usuário local não pertence ao grupo de produção; somente
            # fchown é simulado. Gravação, chmod e replace são reais.
            with patch('baixar_anexos_imagens.os.fchown') as chown:
                for numero in ('1234', '5678'):
                    self.assertTrue(salvar_propriedades(numero, {'propriedades': {}}, arquivo))
                    self.assertEqual(stat.S_IMODE(arquivo.stat().st_mode), 0o640)
                self.assertEqual(chown.call_count, 2)
                self.assertEqual(chown.call_args.args[1:], (-1, grp.getgrnam('www-data').gr_gid))
            self.assertEqual(set(carregar_ocorrencias(arquivo)['ocorrencias']), {'1234', '5678'})

    def test_falha_de_permissao_preserva_json_anterior_e_remove_temporario(self):
        with tempfile.TemporaryDirectory() as directory:
            arquivo = Path(directory) / 'ocorrencias.json'
            original = '{"ocorrencias": {"1234": {}}}\n'
            arquivo.write_text(original)
            with patch('baixar_anexos_imagens.os.fchown', side_effect=PermissionError('grupo indisponível')):
                with self.assertRaises(PermissionError):
                    salvar_propriedades('5678', {}, arquivo)
            self.assertEqual(arquivo.read_text(), original)
            self.assertEqual(list(Path(directory).iterdir()), [arquivo])
