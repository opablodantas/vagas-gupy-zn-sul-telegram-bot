import os
import time
import sqlite3
import requests
import signal
import sys
from dotenv import load_dotenv
from datetime import datetime, timedelta

# --- 1. CONFIGURAÇÕES E CAMINHOS ---
DIRETORIO_ATUAL = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(DIRETORIO_ATUAL, '.env'))
CAMINHO_BANCO = os.path.join(DIRETORIO_ATUAL, 'vagas_gupy.db')

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_RS = os.getenv("CHAT_ID_RS")
CHAT_ID_SC = os.getenv("CHAT_ID_SC")
CHAT_ID_PR = os.getenv("CHAT_ID_PR")

# Lista de chats para envio (para compatibilidade com código existente)
CHAT_IDS = [CHAT_ID_RS, CHAT_ID_SC, CHAT_ID_PR]

TRADUCAO_MODELO = {
    "on-site": "Presencial",
    "hybrid": "Híbrido",
    "remote": "Remoto"
}

TRADUCAO_TIPO_VAGA = {
    "vacancy_type_effective": "Efetivo",
    "vacancy_type_apprentice": "Jovem Aprendiz",
    "vacancy_type_internship": "Estágio",
    "vacancy_type_temporary": "Temporário",
    "vacancy_type_freelancer": "Freelancer"
}

# Configuração para limite de vagas no banco
LIMITE_VAGAS_BANCO = 8000

# Flag para controle de interrupção
executando = True

# --- 2. FUNÇÕES DE MENSAGEM DO TELEGRAM ---
def enviar_mensagem_telegram(mensagem, chat_id_especifico=None):
    """Envia uma mensagem para o Telegram. Se chat_id_especifico for None, envia para todos os grupos"""
    if not TOKEN:
        return
    
    chats_para_enviar = [chat_id_especifico] if chat_id_especifico else CHAT_IDS
    
    for chat_id in chats_para_enviar:
        if not chat_id:
            continue
        
        try:
            url_tg = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
            payload_tg = {"chat_id": chat_id, "text": mensagem, "parse_mode": "HTML"}
            requests.post(url_tg, json=payload_tg, timeout=10)
        except Exception as e:
            print(f"❌ Erro ao enviar mensagem para chat {chat_id}: {e}")

def enviar_mensagem_manutencao():
    """Envia mensagem avisando que o bot entrou em manutenção"""
    mensagem = "⚠️ <b>MANUTENÇÃO</b>\n\nO robô de vagas está passando por manutenção no momento. Em breve retornaremos com as atualizações!"
    enviar_mensagem_telegram(mensagem)

def enviar_mensagem_retorno():
    """Envia mensagem avisando que o bot voltou"""
    mensagem = "✅ <b>ROBÔ ATIVADO</b>\n\nManutenção concluída! O robô de vagas está funcionando normalmente novamente e continuará enviando as vagas disponíveis."
    enviar_mensagem_telegram(mensagem)

# --- 3. TRATAMENTO DE INTERRUPÇÃO ---
def signal_handler(signum, frame):
    """Trata sinais de interrupção (Ctrl+C, SIGTERM)"""
    global executando
    print("\n🛑 Recebido sinal de interrupção. Encerrando o bot...")
    executando = False
    enviar_mensagem_manutencao()
    sys.exit(0)

# Registrar os handlers de sinal
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- 4. BANCO DE DADOS ---
def iniciar_banco():
    conn = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vagas_enviadas (
            link TEXT PRIMARY KEY,
            data_publicacao TEXT,
            titulo TEXT,
            estado TEXT
        )
    ''')
    conn.commit()
    return conn, cursor

def limpar_vagas_antigas(cursor, conn):
    """Remove vagas antigas quando o banco atinge o limite máximo"""
    cursor.execute('SELECT COUNT(*) FROM vagas_enviadas')
    total_vagas = cursor.fetchone()[0]
    
    if total_vagas >= LIMITE_VAGAS_BANCO:
        print(f"🗑️ Banco com {total_vagas} vagas. Removendo registros antigos...")
        
        # Remove as vagas mais antigas, mantendo apenas as mais recentes
        vagas_para_remover = total_vagas - LIMITE_VAGAS_BANCO + 100  # Mantém uma margem de segurança
        
        cursor.execute('''
            DELETE FROM vagas_enviadas 
            WHERE rowid IN (
                SELECT rowid FROM vagas_enviadas 
                ORDER BY data_publicacao ASC 
                LIMIT ?
            )
        ''', (vagas_para_remover,))
        
        conn.commit()
        print(f"✅ Removidas {vagas_para_remover} vagas antigas. Banco agora tem {LIMITE_VAGAS_BANCO} vagas.")

# --- 5. MOTOR DE BUSCA DA GUPY (APENAS SC, RS, PR) ---
def buscar_vagas_gupy():
    print("🚀 Iniciando varredura detalhada na API da Gupy...")
    conn, cursor = iniciar_banco()
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://portal.gupy.io'
    }
    
    url_api = "https://employability-portal.gupy.io/api/v1/jobs"
    
    # Filtros para os estados solicitados com seus respectivos Chat IDs
    filtros_de_busca = [
        {"nome": "SANTA CATARINA", "params": {'state': 'Santa Catarina', 'limit': 10}, "chat_id": CHAT_ID_SC},
        {"nome": "RIO GRANDE DO SUL", "params": {'state': 'Rio Grande do Sul', 'limit': 10}, "chat_id": CHAT_ID_RS},
        {"nome": "PARANÁ", "params": {'state': 'Paraná', 'limit': 10}, "chat_id": CHAT_ID_PR}
    ]

    vagas_enviadas_ciclo = 0

    for filtro in filtros_de_busca:
        print(f"\n🔎 Varrendo vagas para: {filtro['nome']}...")
        
        vagas_velhas = 0
        LIMITE_VELHAS = 20
        PAGINA_MAXIMA = 35
        
        for pagina in range(1, PAGINA_MAXIMA + 1):
            if not executando:
                conn.close()
                return
            
            print(f"   ⏳ Lendo página {pagina} de {PAGINA_MAXIMA}...")
            
            offset = (pagina - 1) * 10
            
            params_atuais = filtro['params'].copy()
            params_atuais['offset'] = offset
            
            try:
                resposta = requests.get(url_api, headers=headers, params=params_atuais, timeout=15)
                
                if resposta.status_code != 200: 
                    print(f"🛑 Erro de conexão. Código HTTP: {resposta.status_code}")
                    break
                
                try:
                    dados_json = resposta.json()
                except Exception:
                    print(f"🛑 Fomos bloqueados! O servidor não enviou os dados JSON.")
                    break

                lista_vagas = dados_json.get('data', [])
                if not lista_vagas: 
                    print("   🔚 Não há mais vagas disponíveis nesta busca.")
                    break

                for vaga in lista_vagas:
                    if not executando:
                        conn.close()
                        return
                    
                    link_vaga = vaga.get('jobUrl', '')
                    if not link_vaga: continue
                    
                    titulo = vaga.get('name', 'Título Indisponível')
                    empresa = vaga.get('careerPageName', 'Empresa não informada')
                    
                    # Localização específica
                    cidade = vaga.get('city', '')
                    estado = vaga.get('state', '')
                    local = f"{cidade} - {estado}" if cidade and estado else estado or "Local não informado"
                    
                    modelo = TRADUCAO_MODELO.get(vaga.get('workplaceType', ''), "Não informado")
                    tipo = TRADUCAO_TIPO_VAGA.get(vaga.get('type', ''), "Outros")
                    pcd = "Sim" if vaga.get('disabilities') else "Não informado"

                    data_iso = vaga.get('publishedDate', '')
                    try:
                        data_limpa = data_iso.split('.')[0] 
                        data_utc = datetime.strptime(data_limpa, "%Y-%m-%dT%H:%M:%S")
                        data_brt = data_utc - timedelta(hours=3)
                        data_f = data_brt.strftime("%d/%m/%Y")
                        hora_f = data_brt.strftime("%H:%M")
                    except Exception:
                        data_f, hora_f = "Sem data", "--:--"

                    cursor.execute('SELECT 1 FROM vagas_enviadas WHERE link = ?', (link_vaga,))
                    if cursor.fetchone():
                        vagas_velhas += 1
                        if vagas_velhas >= LIMITE_VELHAS: 
                            break
                    else:
                        vagas_velhas = 0 
                        cursor.execute('INSERT INTO vagas_enviadas VALUES (?, ?, ?, ?)', 
                                     (link_vaga, data_f, titulo, estado))
                        conn.commit()
                        vagas_enviadas_ciclo += 1
                        
                        # Verifica e limpa vagas antigas a cada 10 novas vagas
                        if vagas_enviadas_ciclo % 10 == 0:
                            limpar_vagas_antigas(cursor, conn)
                        
                        titulo_mensagem = f"🎯 <b>VAGA GUPY - {filtro['nome']}!</b>"
                        mensagem = f"{titulo_mensagem}\n\n" \
                                   f"💼 <b>Vaga:</b> {titulo}\n" \
                                   f"🏢 <b>Empresa:</b> {empresa}\n" \
                                   f"📍 <b>Local:</b> {local}\n" \
                                   f"💻 <b>Modelo:</b> {modelo}\n" \
                                   f"📄 <b>Tipo:</b> {tipo}\n" \
                                   f"♿ <b>PCD:</b> {pcd}\n" \
                                   f"📅 <b>Data:</b> {data_f} às {hora_f}\n\n" \
                                   f"🔗 <a href='{link_vaga}'>Clique aqui para se candidatar na plataforma</a>"

                        # Envia para o chat específico do estado
                        chat_id_destino = filtro['chat_id']
                        if chat_id_destino:
                            url_tg = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
                            payload_tg = {"chat_id": chat_id_destino, "text": mensagem, "parse_mode": "HTML", "disable_web_page_preview": True}
                            
                            try:
                                r = requests.post(url_tg, json=payload_tg, timeout=10)
                                if r.status_code == 200:
                                    print(f"✅ Enviada ({filtro['nome']}): {titulo[:40]}...")
                            except Exception as e:
                                print(f"❌ Erro ao enviar para o Telegram: {e}")
                        else:
                            print(f"⚠️ Chat ID não configurado para {filtro['nome']}")
                        
                        time.sleep(2)
                
                if vagas_velhas >= LIMITE_VELHAS:
                    print(f"   🛑 Muitas vagas antigas ({LIMITE_VELHAS}). Pulando para a próxima busca.")
                    break 

            except Exception as e:
                print(f"⚠️ Erro de execução: {e}")
                break

    # Limpeza final
    limpar_vagas_antigas(cursor, conn)
    conn.close()
    print(f"\n✅ Varredura finalizada! {vagas_enviadas_ciclo} novas vagas enviadas.")
    return vagas_enviadas_ciclo

# --- 6. LOOP PRINCIPAL COM EXECUÇÃO CONTÍNUA ---
def main():
    global executando
    
    if not TOKEN:
        print("❌ ERRO: Token do Telegram não encontrado no arquivo .env!")
        return
    
    # Verifica se pelo menos um Chat ID está configurado
    chats_configurados = [chat for chat in CHAT_IDS if chat]
    if not chats_configurados:
        print("❌ ERRO: Nenhum Chat ID configurado no arquivo .env!")
        return
    
    print("🤖 Bot de vagas Gupy iniciado!")
    print("📌 Monitorando vagas para os estados: Santa Catarina, Rio Grande do Sul e Paraná")
    print("📊 Limite de vagas no banco:", LIMITE_VAGAS_BANCO)
    print("⏰ O bot ficará em execução contínua, verificando novas vagas a cada 5 minutos")
    print("🔴 Para parar o bot, pressione Ctrl+C\n")
    print(f"📱 Chats configurados: {len(chats_configurados)}")
    
    # Envia mensagem de inicialização
    enviar_mensagem_retorno()
    
    ciclo = 0
    
    while executando:
        ciclo += 1
        print(f"\n{'='*50}")
        print(f"🔄 CICLO #{ciclo} - {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        print(f"{'='*50}")
        
        try:
            vagas_enviadas = buscar_vagas_gupy()
            
            if vagas_enviadas == 0:
                print("📭 Nenhuma vaga nova encontrada neste ciclo.")
            
            # Aguarda 5 minutos antes da próxima verificação
            print(f"\n⏳ Aguardando 5 minutos até a próxima verificação...")
            for _ in range(300):  # 300 segundos = 5 minutos
                if not executando:
                    break
                time.sleep(1)
                
        except Exception as e:
            print(f"❌ Erro crítico no ciclo #{ciclo}: {e}")
            # Aguarda 1 minuto antes de tentar novamente em caso de erro
            time.sleep(60)

if __name__ == '__main__':
    main()