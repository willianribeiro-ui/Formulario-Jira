"""
criar_pec_formulario.py
Recebe o payload do formulário HTML via HTTP POST e cria uma PEC no Jira.

Equivalente ao migrar_bitrix_para_jira.py, porém a fonte de dados
é o formulário HTML (JSON via webhook) em vez do Bitrix.

Payload esperado (JSON):
  {
    "nome_agente":      "João Silva",
    "departamento":     "Suporte",
    "canal":            ["WhatsApp"],
    "empresa":          "1042 - Contabilidade Silva Ltda",
    "categoria":        "A",
    "pedido_diretoria": "Não",
    "em_implantacao":   "Não",
    "ultima_entrega":   "10/04/2025",
    "segmento":         "Contabilidade",
    "responsavel":      "Maria Souza",
    "email":            "maria@empresa.com.br",
    "telefone":         "(11) 99999-9999",
    "titulo_pec":       "Ajuste na apuração de ICMS para grupo não contábil",
    "descricao":        "...",
    "problema":         "...",
    "resultado":        "...",
    "modulos":          ["Apuração", "SPED Fiscal"],
    "legislacao":       "Sim",
    "detalhes_leg":     "NT 2024.001",
    "passo_a_passo":    "1. Acesse..."
  }

Uso direto (teste via linha de comando):
    python criar_pec_formulario.py --payload payload.json
    python criar_pec_formulario.py --payload payload.json --dry-run

Uso como servidor HTTP (porta 5000):
    python criar_pec_formulario.py --server
    python criar_pec_formulario.py --server --port 8080
"""
import os
import re
import sys
import json
import time
import base64
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env', override=True)
except Exception:
    pass

if sys.platform.startswith('win'):
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
JIRA_USER    = os.getenv('JIRA_USER', '')
JIRA_TOKEN   = os.getenv('JIRA_TOKEN', '')
JIRA_DOMAIN  = os.getenv('JIRA_DOMAIN', 'sisaudcon.atlassian.net')
JIRA_PROJECT = 'PEC'

# ID da opção fallback de Empresa Cliente no Jira (HOSAAM - SAAM - TIME DEV - PO)
EMPRESA_FALLBACK_ID = '12381'

# Marcador para evitar duplicatas
FORM_MARKER = '[FORM-PEC]'

# Mapeamento de módulos do formulário → IDs das opções do Jira (customfield_10126)
MODULO_MAP = {
    'SPED Fiscal':        '10277',
    'SPED Contribuições': '10278',
    'REINF':              '10281',
    'EFD ICMS/IPI':       '10277',
    'Escrita Fiscal':     '10277',
    'XML':                '10284',
    'Importação':         '10287',
    'Calculadora':        '10285',
    'Dashboard':          '10285',
    'Relatórios':         '10285',
    'Auditoria':          '10282',
    'NF-e':               '10284',
    'NFC-e':              '10284',
    'CT-e':               '10284',
    'Apuração':           '10277',
    'Automação':          '10297',
    'API':                '10297',
    'Outro':              '10297',
}

# Mapeamento de categorias do formulário → prioridades Jira
CATEGORIA_PRIORIDADE = {
    'A+': 'Highest',
    'A':  'High',
    'B':  'Medium',
    'C':  'Low',
    'D':  'Lowest',
}

# ---------------------------------------------------------------------------
# JIRA AUTH
# ---------------------------------------------------------------------------
_b64 = base64.b64encode(f'{JIRA_USER}:{JIRA_TOKEN}'.encode()).decode()
JIRA_HEADERS = {
    'Authorization': f'Basic {_b64}',
    'Accept':        'application/json',
    'Content-Type':  'application/json',
}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def log(msg):
    print(f'    {msg}', flush=True)


def section(title):
    print(f'\n[{title}]', flush=True)


def _adf_doc(paragraphs: list[str]) -> dict:
    """Monta documento ADF a partir de lista de strings."""
    content = [
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': p}]}
        for p in paragraphs if p and p.strip()
    ]
    if not content:
        content = [{'type': 'paragraph', 'content': []}]
    return {'version': 1, 'type': 'doc', 'content': content}


def _adf_heading(text: str, level: int = 3) -> dict:
    return {
        'type': 'heading',
        'attrs': {'level': level},
        'content': [{'type': 'text', 'text': text}],
    }


def _adf_para(text: str) -> dict:
    return {'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}


# ---------------------------------------------------------------------------
# JIRA API
# ---------------------------------------------------------------------------
def jira_get(path, params=None):
    r = requests.get(f'https://{JIRA_DOMAIN}{path}',
                     headers=JIRA_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def jira_post(path, body):
    return requests.post(f'https://{JIRA_DOMAIN}{path}',
                         headers=JIRA_HEADERS, json=body, timeout=30)


def resolve_issue_type(project_key: str) -> tuple[str | None, str | None]:
    """Descobre o ID do tipo de issue mais adequado no projeto."""
    try:
        data = jira_get(f'/rest/api/3/project/{project_key}')
        issue_types = data.get('issueTypes', [])
    except Exception:
        issue_types = []

    if not issue_types:
        try:
            meta = jira_get('/rest/api/3/issue/createmeta', params={
                'projectKeys': project_key,
                'expand': 'projects.issuetypes',
            })
            projects = meta.get('projects', [])
            if projects:
                issue_types = projects[0].get('issuetypes', [])
        except Exception:
            pass

    if not issue_types:
        return None, None

    by_name = {t['name']: t for t in issue_types if not t.get('subtask', False)}
    for preferred in ('PEC', 'Melhoria', 'Tarefa', 'Task', 'Story', 'Improvement'):
        if preferred in by_name:
            t = by_name[preferred]
            return t['id'], t['name']

    for t in issue_types:
        if not t.get('subtask', False):
            return t['id'], t['name']

    return None, None


def load_empresa_cliente_options() -> tuple[dict, dict]:
    """
    Retorna dois índices das opções de Empresa Cliente do projeto PEC:
      code_map : {'000068': {'id': '11696', 'value': '...'}, ...}
      name_map : {'NOME NORMALIZADO': {'id': ..., 'value': ...}, ...}
    """
    try:
        data = jira_get('/rest/api/3/issue/createmeta', params={
            'projectKeys': JIRA_PROJECT,
            'issuetypeIds': '10009',
            'expand': 'projects.issuetypes.fields',
        })
        options = (data['projects'][0]['issuetypes'][0]['fields']
                   .get('customfield_10274', {}).get('allowedValues', []))
    except Exception as e:
        log(f'AVISO: nao foi possivel carregar opcoes de Empresa Cliente: {e}')
        return {}, {}

    code_map, name_map = {}, {}
    for o in options:
        raw = o['value']
        m = re.match(r'^(\d{6})', raw.strip())
        if m:
            code_map[m.group(1)] = {'id': o['id'], 'value': raw}
        parts = raw.split(' - ', 1)
        if len(parts) == 2:
            nome = re.sub(r'[^A-Z0-9 ]', '', parts[1].upper().strip())
            name_map[nome] = {'id': o['id'], 'value': raw}
    return code_map, name_map


def resolve_empresa_cliente(empresa_str: str,
                             code_map: dict,
                             name_map: dict) -> list[dict]:
    """
    Tenta encontrar a Empresa Cliente no Jira pelo código de 6 dígitos
    ou pelo nome contido na string 'empresa' do formulário.
    Fallback: HOSAAM - SAAM - TIME DEV - PO.
    """
    found = {}

    # 1. Código de 6 dígitos
    codes = re.findall(r'\b(\d{6})\b', empresa_str)
    for code in codes:
        if code in code_map:
            opt = code_map[code]
            found[opt['id']] = opt

    # 2. Match por nome (parte após o traço, se houver)
    if ' - ' in empresa_str:
        nome_raw = empresa_str.split(' - ', 1)[1].strip()
    else:
        nome_raw = empresa_str.strip()

    nome_norm = re.sub(r'[^A-Z0-9 ]', '', nome_raw.upper())
    if nome_norm in name_map:
        opt = name_map[nome_norm]
        found[opt['id']] = opt

    # 3. Fallback
    if not found:
        found[EMPRESA_FALLBACK_ID] = {'id': EMPRESA_FALLBACK_ID, 'value': 'fallback'}

    return [{'id': k} for k in found]


def pec_already_exists(titulo: str, empresa: str) -> str | None:
    """
    Verifica via JQL se já existe uma PEC com o mesmo título e marcador de formulário.
    Retorna a chave Jira (ex: PEC-999) ou None.
    """
    titulo_safe = titulo.replace('"', '\\"')[:80]
    jql = f'project = {JIRA_PROJECT} AND summary ~ "{titulo_safe}" AND text ~ "{FORM_MARKER}"'
    try:
        resp = jira_post('/rest/api/3/search/jql', {
            'jql': jql,
            'fields': ['summary'],
            'maxResults': 1,
        })
        if resp.status_code == 200:
            issues = resp.json().get('issues', [])
            if issues:
                return issues[0].get('key')
    except Exception as e:
        log(f'AVISO: falha ao verificar duplicata: {e}')
    return None


def build_description(payload: dict) -> dict:
    """
    Monta a descrição ADF estruturada com todas as informações do formulário.
    """
    content = []

    # Cabeçalho de rastreabilidade
    content.append(_adf_para(
        f'{FORM_MARKER} Criado via formulário de solicitação em '
        f'{datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")} UTC'
    ))

    # Bloco: Agente
    content.append(_adf_heading('📋 Dados do Agente', 3))
    content.append(_adf_para(f'Agente: {payload.get("nome_agente", "")}'))
    content.append(_adf_para(f'Departamento: {payload.get("departamento", "")}'))
    canal = ', '.join(payload.get('canal', [])) or '—'
    content.append(_adf_para(f'Canal de chegada: {canal}'))

    # Bloco: Empresa
    content.append(_adf_heading('🏢 Dados da Empresa', 3))
    content.append(_adf_para(f'Empresa: {payload.get("empresa", "")}'))
    content.append(_adf_para(f'Categoria: {payload.get("categoria", "")}'))
    content.append(_adf_para(f'Segmento: {payload.get("segmento", "")}'))
    content.append(_adf_para(
        f'Pedido da diretoria: {payload.get("pedido_diretoria", "")} | '
        f'Em implantação: {payload.get("em_implantacao", "")} | '
        f'Última entrega: {payload.get("ultima_entrega", "")}'
    ))

    # Bloco: Solicitante
    content.append(_adf_heading('👤 Dados do Solicitante', 3))
    content.append(_adf_para(f'Nome: {payload.get("responsavel", "")}'))
    content.append(_adf_para(f'E-mail: {payload.get("email", "")}'))
    content.append(_adf_para(f'Telefone: {payload.get("telefone", "")}'))

    # Bloco: Solicitação
    content.append(_adf_heading('🛠️ Descrição da Melhoria', 3))
    if payload.get('descricao'):
        content.append(_adf_para(payload['descricao']))

    if payload.get('problema'):
        content.append(_adf_heading('Problema atual', 4))
        content.append(_adf_para(payload['problema']))

    if payload.get('resultado'):
        content.append(_adf_heading('Resultado esperado', 4))
        content.append(_adf_para(payload['resultado']))

    # Bloco: Impacto técnico
    content.append(_adf_heading('⚙️ Impacto Técnico', 3))
    modulos = ', '.join(payload.get('modulos', [])) or '—'
    content.append(_adf_para(f'Módulos: {modulos}'))
    content.append(_adf_para(
        f'Legislação envolvida: {payload.get("legislacao", "Não")}'
    ))
    if payload.get('detalhes_leg'):
        content.append(_adf_para(f'Detalhes legislação: {payload["detalhes_leg"]}'))

    # Bloco: Passo a passo
    if payload.get('passo_a_passo'):
        content.append(_adf_heading('🔁 Passo a Passo para Reproduzir', 3))
        content.append(_adf_para(payload['passo_a_passo']))

    return {'version': 1, 'type': 'doc', 'content': content}


def resolve_modulos(modulos_list: list[str]) -> list[dict]:
    """Converte lista de nomes de módulos em lista de {id} para o Jira."""
    ids_vistos = set()
    result = []
    for nome in modulos_list:
        mid = MODULO_MAP.get(nome)
        if mid and mid not in ids_vistos:
            result.append({'id': mid})
            ids_vistos.add(mid)
    if not result:
        result = [{'id': MODULO_MAP['Outro']}]
    return result[:4]


def create_jira_pec(payload: dict,
                    issue_type_id: str,
                    empresa_options: list[dict],
                    dry_run: bool = False) -> str | None:
    """
    Cria a issue no projeto PEC e retorna a chave (ex: PEC-123) ou None.
    """
    titulo = (payload.get('titulo_pec') or '(sem título)')[:255]

    if dry_run:
        empresas = ', '.join(e.get('id', '') for e in empresa_options)
        log(f'[DRY-RUN] Criaria PEC: {titulo[:70]}')
        log(f'[DRY-RUN] Empresa Cliente: {empresas}')
        log(f'[DRY-RUN] Módulos: {payload.get("modulos", [])}')
        return f'PEC-DRY-{int(time.time())}'

    descricao_adf = build_description(payload)
    modulos_field = resolve_modulos(payload.get('modulos', []))

    # Prioridade com base na categoria
    prioridade = CATEGORIA_PRIORIDADE.get(payload.get('categoria', ''), 'Medium')

    body = {
        'fields': {
            'project':           {'key': JIRA_PROJECT},
            'issuetype':         {'id': issue_type_id},
            'summary':           titulo,
            'description':       descricao_adf,
            'customfield_10274': empresa_options,   # Empresa Cliente
            'customfield_10126': modulos_field,     # Módulo SAAM
            'priority':          {'name': prioridade},
        }
    }

    resp = jira_post('/rest/api/3/issue', body)
    if resp.status_code in (200, 201):
        return resp.json().get('key')

    log(f'ERRO ao criar PEC (HTTP {resp.status_code}): {resp.text[:300]}')
    return None


def post_comment_jira(pec_key: str, payload: dict, dry_run: bool = False) -> bool:
    """Adiciona comentário na PEC com os dados do agente/solicitante para rastreio."""
    if dry_run:
        log(f'[DRY-RUN] Comentário Jira {pec_key}: dados de rastreio')
        return True

    agente = payload.get('nome_agente', 'N/I')
    depto  = payload.get('departamento', 'N/I')
    canal  = ', '.join(payload.get('canal', [])) or 'N/I'
    resp_nome  = payload.get('responsavel', 'N/I')
    resp_email = payload.get('email', 'N/I')
    resp_tel   = payload.get('telefone', 'N/I')

    body = {
        'body': {
            'version': 1, 'type': 'doc',
            'content': [
                {
                    'type': 'paragraph',
                    'content': [
                        {'type': 'text', 'text': f'{FORM_MARKER} ',
                         'marks': [{'type': 'strong'}]},
                        {'type': 'text', 'text': 'Solicitação criada via formulário HTML'},
                    ],
                },
                _adf_para(f'Agente: {agente} ({depto}) — Canal: {canal}'),
                _adf_para(f'Contato: {resp_nome} | {resp_email} | {resp_tel}'),
            ],
        }
    }
    resp = jira_post(f'/rest/api/3/issue/{pec_key}/comment', body)
    return resp.status_code in (200, 201)


# ---------------------------------------------------------------------------
# CORE
# ---------------------------------------------------------------------------
def processar_payload(payload: dict, dry_run: bool = False) -> dict:
    """
    Processa um payload do formulário e cria a PEC no Jira.
    Retorna dict com resultado: {'pec_key', 'status', 'msg'}.
    """
    print('=' * 65)
    mode = ' [DRY-RUN]' if dry_run else ''
    print(f'SAAM-Agente — Formulário -> Jira{mode}')
    print('=' * 65)

    # Validações mínimas
    titulo = (payload.get('titulo_pec') or '').strip()
    if not titulo:
        return {'pec_key': None, 'status': 'erro', 'msg': 'Campo titulo_pec obrigatório'}

    empresa_str = (payload.get('empresa') or '').strip()
    if not empresa_str:
        return {'pec_key': None, 'status': 'erro', 'msg': 'Campo empresa obrigatório'}

    # Verificar credenciais
    missing = [k for k, v in [('JIRA_USER', JIRA_USER), ('JIRA_TOKEN', JIRA_TOKEN)] if not v]
    if missing:
        return {
            'pec_key': None, 'status': 'erro',
            'msg': f'Variáveis não configuradas no .env: {", ".join(missing)}'
        }

    # 1. Tipo de issue
    section('1/4 Verificando projeto Jira')
    try:
        issue_type_id, issue_type_name = resolve_issue_type(JIRA_PROJECT)
    except requests.HTTPError as e:
        return {'pec_key': None, 'status': 'erro', 'msg': f'Erro ao acessar Jira: {e}'}

    if not issue_type_id:
        return {'pec_key': None, 'status': 'erro', 'msg': 'Nenhum tipo de issue encontrado'}

    log(f'Tipo de issue: "{issue_type_name}" (id={issue_type_id})')

    # 2. Empresa Cliente
    section('2/4 Resolvendo Empresa Cliente')
    code_map, name_map = load_empresa_cliente_options()
    empresa_options = resolve_empresa_cliente(empresa_str, code_map, name_map)
    log(f'Opções encontradas: {[e["id"] for e in empresa_options]}')

    # 3. Verificar duplicata
    section('3/4 Verificando duplicata')
    existing = pec_already_exists(titulo, empresa_str)
    if existing:
        msg = f'PEC já existe: {existing}'
        log(msg)
        return {'pec_key': existing, 'status': 'pulado', 'msg': msg}

    # 4. Criar PEC
    section('4/4 Criando PEC no Jira')
    log(f'Título: {titulo[:70]}')
    log(f'Agente: {payload.get("nome_agente", "N/I")} | '
        f'Depto: {payload.get("departamento", "N/I")}')
    log(f'Empresa: {empresa_str[:50]}')
    log(f'Módulos: {payload.get("modulos", [])}')

    pec_key = create_jira_pec(payload, issue_type_id, empresa_options, dry_run=dry_run)
    if not pec_key:
        return {'pec_key': None, 'status': 'erro', 'msg': 'Falha ao criar PEC no Jira'}

    log(f'PEC criada: {pec_key}')

    ok_c = post_comment_jira(pec_key, payload, dry_run=dry_run)
    log(f'Comentário Jira: {"OK" if ok_c else "ERRO"}')

    jira_url = f'https://{JIRA_DOMAIN}/browse/{pec_key}'
    print(f'\n  ✅ PEC criada com sucesso: {pec_key}')
    print(f'  🔗 {jira_url}')
    print('\n' + '=' * 65)

    return {
        'pec_key':  pec_key,
        'jira_url': jira_url,
        'status':   'criado',
        'msg':      f'PEC {pec_key} criada com sucesso',
    }


# ---------------------------------------------------------------------------
# ENTRYPOINTS
# ---------------------------------------------------------------------------
def run_from_file(path: str, dry_run: bool):
    """Modo CLI: lê o payload de um arquivo JSON e processa."""
    with open(path, encoding='utf-8') as f:
        payload = json.load(f)
    resultado = processar_payload(payload, dry_run=dry_run)
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


def run_server(host: str = '0.0.0.0', port: int = 5000):
    """
    Modo servidor: expõe endpoint POST /pec.
    Requer: pip install flask flask-cors
    """
    try:
        from flask import Flask, request, jsonify
        from flask_cors import CORS
    except ImportError:
        print('ERRO: instale as dependências: pip install flask flask-cors')
        sys.exit(1)

    app = Flask(__name__)
    CORS(app)  # libera todas as origens (ideal para testes)
    # Para produção, restrinja ao domínio do Lovable:
    # CORS(app, origins=["https://seuapp.lovable.app"])

    @app.route('/pec', methods=['POST', 'OPTIONS'])
    def criar_pec():
        if request.method == 'OPTIONS':
            # Preflight CORS
            return '', 204
        payload = request.get_json(force=True, silent=True) or {}
        dry_run = request.args.get('dry_run', 'false').lower() == 'true'
        resultado = processar_payload(payload, dry_run=dry_run)
        status_code = 201 if resultado['status'] == 'criado' else (
            200 if resultado['status'] == 'pulado' else 400
        )
        return jsonify(resultado), status_code

    @app.route('/health', methods=['GET'])
    def health():
        return jsonify({'status': 'ok', 'project': JIRA_PROJECT}), 200

    print(f'Servidor iniciado em http://{host}:{port}')
    print(f'Endpoint: POST http://{host}:{port}/pec')
    print(f'Health:   GET  http://{host}:{port}/health')
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Cria PEC no Jira a partir do payload do formulário HTML.'
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument('--payload', type=str,
                            help='Caminho para arquivo JSON com o payload do formulário')
    mode_group.add_argument('--server', action='store_true',
                            help='Inicia servidor HTTP (Flask) na porta especificada')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simula sem criar nada no Jira')
    parser.add_argument('--port', type=int, default=5000,
                        help='Porta do servidor HTTP (padrão: 5000)')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                        help='Host do servidor HTTP (padrão: 0.0.0.0)')
    args = parser.parse_args()

    if args.server:
        run_server(host=args.host, port=args.port)
    else:
        run_from_file(args.payload, dry_run=args.dry_run)
