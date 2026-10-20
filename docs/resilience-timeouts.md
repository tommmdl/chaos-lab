# Os seis segundos, agora com captura na matriz

[English](resilience-timeouts.en.md) · [Investigação anterior](pod-replacement.md)

Os dois CIs anteriores passaram nas checagens de recuperação, mas a matriz ainda registrou timeouts de cerca de seis segundos. As capturas daquela etapa cobriam outro teste, executado antes da matriz. Elas não permitiam explicar essas requisições.

Esta investigação coloca a captura na própria matriz, com os quatro perfis: sem/com drenagem, combinados com remoção abrupta/prazo de 30 segundos. Mantém quatro rodadas por perfil, dois trabalhadores por acesso e a mesma política de tentativas para GET. O timeout por operação de socket do acesso direto continua em seis segundos; o teste não foi acelerado para esconder a espera.

## O que passou a ser observado

A sonda em [resilience.py](../tests/resilience.py) guarda os horários de início e término, a porta de origem, o tempo de conexão e a fase que falhou: conexão, envio, cabeçalhos ou corpo. O conteúdo da requisição, a leitura da resposta e os limites continuam os mesmos. O ponto de criação do socket é instrumentado para reter sua porta mesmo se a conexão não terminar; os destinos do lab são endereços IPv4 numéricos.

O CI captura o tráfego da NodePort `30434` nos nós kind e no host do runner. Também captura TCP da porta `8000` dos IPs iniciais da API. Os arquivos têm cabeçalho Linux SLL2 e snapshot de 60 bytes, sem conteúdo HTTP. Endereços de Pods criados depois do início da tentativa não entram no filtro de backend; sua ausência nessa captura não prova descarte de pacotes.

A análise em [resilience_packets.py](../scripts/resilience_packets.py) cruza a tupla e o intervalo de cada requisição direta que falhou ou levou pelo menos 500 ms. Guarda ainda uma requisição saudável por tentativa. Busca SYN, SYN-ACK, dados enviados, ACKs, dados de resposta e RST. Usa sequência e horário do SYN para listar candidatos de tradução para os Pods iniciais. ACK não prova que o processo da aplicação aceitou ou leu a requisição.

Todas as amostras HTTP continuam no relatório. O cliente do acesso com tentativas conecta ao proxy local: sua porta de loopback não identifica cada tentativa interna na NodePort. Esses erros e contagens continuam visíveis, mas não são atribuídos a uma conexão específica sem evidência.

## Controles reais, separados da aplicação

[resilience_capture.py](../tests/resilience_capture.py) testa dois casos conhecidos com o mesmo cliente de seis segundos:

- Um servidor local recebe a requisição e não envia resposta. A espera deve terminar nos cabeçalhos, com conexão estabelecida e sem dados de resposta.
- Uma regra temporária descarta SYNs para uma porta comprovadamente sem serviço no nó kind. A espera deve terminar na conexão, com SYNs e sem SYN-ACK. A regra tem identificador próprio e é removida ao final.

Esses controles conferem a leitura da captura. Não contam como reprodução do timeout da API. A matriz continua usando as falhas reais do Chaos Mesh, com comparação dos dados originais, PVC e recursos dos outros labs.

## Resultados

A [primeira matriz local](../evidence/matrix-timeouts-local-resilience.json) passou nas 39 verificações ao longo de 16 remoções em 290,38 segundos. Essa primeira captura observou os nós kind; a observação no runner foi acrescentada para o CI. Nenhum timeout de seis segundos ocorreu nesta matriz. A [análise](../evidence/matrix-timeouts-local-resilience-timeouts.json) guardou 15 observações de reset entre requisições diretas com erro ou lentidão e uma requisição bem-sucedida de 4,086 segundos cuja tupla não teve correspondência dentro do intervalo registrado. Há um fluxo com a mesma porta fora desse intervalo; ele não foi deslocado nem atribuído por suposição. A lacuna continua sem explicação. As comparações das linhas, timestamps, identidade do PVC e recursos dos outros labs passaram.

Os [sete controles locais](../evidence/matrix-timeouts-local-resilience-controls.json) passaram. As duas esperas de seis segundos foram observadas e [classificadas pelos pacotes](../evidence/matrix-timeouts-local-resilience-controls-analysis.json): uma na conexão e outra aguardando cabeçalhos de resposta. Os timeouts desses controles não entram como reprodução da API.

Os resultados do CI serão acrescentados após a execução, incluindo tentativas que falharem e casos em que o timeout não aparecer.

## Repetir

Execute uma suíte de cada vez, com o ambiente do lab instalado:

```bash
python3 tests/resilience_capture.py
python3 tests/resilience.py --rounds 4 --capture-packets
python3 scripts/archive_packets.py --report evidence/resilience.json --output evidence/matrix-packet-archive.json
python3 scripts/analyze_resilience.py evidence/resilience.json --output evidence/resilience-timeouts.json
```

A captura exige `tcpdump` e permissão para entrar nos namespaces de rede dos nós. No WSL usado neste lab, `PACKET_PRIVILEGE=wsl` seleciona o acesso root; no CI, o runner usa `sudo`. Guarde cada execução com outro nome antes de repetir: os nomes canônicos em `evidence` são saídas da execução atual.

A análise offline verifica os hashes dos PCAPs, formato sem payload, contagem de quadros, drops e igualdade com a análise feita durante a execução. Se faltar evidência ou houver perda na coleta, isso aparece no relatório; não vira uma conclusão sobre o componente responsável.

Referências: [timeout do HTTPConnection](https://docs.python.org/3/library/http.client.html) e [encerramento de Pods e seus endpoints](https://kubernetes.io/docs/tutorials/services/pods-and-endpoint-termination-flow/).
