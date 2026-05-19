para calcular a distancia de finetti para uma partida devemos realizar o seguinte processo:

peimeiramente devemos montar uma tabela de probabilidade dos time marcar determinado numero de gols, para isso utilizamos poisson para realizar esse calculo, ou seja
para cada time tomamos a media de gols esperados (xG) e aplicamos na formula de poisson onde:
lambda = xG
k = numeros de gols (vai de 0 até 5)
daí montamos uma tabela da seguinte forma:
gols|0|1|2|3|4|5
time 1|probabilidade do time 1 fazer 0 gols|...
time 2|probabilidade do time 2 fazer 0 gols|...

feito isso agora vamos montar uma tabela de probabilidade dos possiveis placares para cada time, ou seja mostamos a seguinte tabela:
    0, 1, 2, 3, 4, 5,
0   ., ., ., ., ., .,    
1   ., ., ., ., ., .,
2   ., ., ., ., ., ., 
3   ., ., ., ., ., .,
4   ., ., ., ., ., ., 
5   ., ., ., ., ., .,

onde cada ponto representa o produto da probabilidade do time 1 fazer x gols com a probabilidade do time 2 fazer y gols
onde essas probabilidades são aquelas calculada anteriormente, feito isso temos que a soma dos elementos da diagonal superior é probabilidade do time 1 ganhar, a soma dos elementos da diagonal inferior é a probabilidade do time 2 ganhar o a soma dos elementos da diagonal é a probabilidade de ocorrer empate
feito isso temos a tabela

time1 |prob t1
empate|prob empate
time2 |prob t2

tendo essas probabilidades podemos então podemos então calcular a distancia de finetti 
que será representado na seguinte matriz

time 1(casa) | (prob t1 - 1)² + (prob empate - 0)² + (prob t2 - 0)²
empate       | (prob t1 - 0)² + (prob empate - 1)² + (prob t2 - 0)²
time 2(fora) | (prob t1 - 0)² + (prob empate - 0)² + (prob t2 - 1)²
