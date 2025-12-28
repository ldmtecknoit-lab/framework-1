from framework.service.context import container
from dependency_injector import providers
import tomli
import sys
import os
from jinja2 import Environment
import asyncio
import ast
import re
import fnmatch
from datetime import datetime, timezone
import uuid
import json
import copy
from urllib.parse import parse_qs,urlencode,urlparse
import types 
import inspect
import contextvars
from cerberus import Validator, TypeDefinition, errors
import functools
from typing import Dict, Any, Optional, List, Callable
from lark import Lark, Transformer, v_args, Token
import mistql

# Importa le funzioni di flusso e utilità che erano implicitamente usate o attese
from framework.service.flow import (
    asynchronous, 
    synchronous, 
    get_transaction_id, 
    set_transaction_id,
    _transaction_id,
    convert,
    get,      # Re-exported
    put,      # Re-exported
    format,   # Re-exported
    transform as translation, # Aliased for backward compatibility/tests
    route,
    normalize,
    framework_log
)
import framework.service.flow as flow

# =====================================================================
# --- 1. Definizione della Grammatica (DSL Rules) - CORRETTA V18 ---
# =====================================================================

grammar = r"""
    start:  [dictionary] 

    // --- TOKEN ---
    // Operatori aritmetici
    POW_OP: "^"
    MUL_OP: "*"
    DIV_OP: "/"
    MOD_OP: "%"
    ADD_OP: "+"
    SUB_OP: "-"
    COMPARISON_OP: "==" | "!=" | ">=" | "<=" | ">" | "<"
    PIPE: "|"
    #LPAR: "("
    #RPAR: ")"
    #LBRACE: "{"
    #RBRACE: "}"
    #COMMA: ","
    #COLON: ":"
    #SEMICOLON: ";"
    QUALIFIED_CNAME: CNAME ("." CNAME)+
    COMMENT: /#[^\n]*/
    #NATURAL: natural
    #INTEGER: integer
    #RATIONAL: rational
    #IRRATIONAL: irrational
    #REAL: real
    #COMPLEX: complex
    #BOOLEAN: boolean
    #STRING: string

    property_access: (CNAME | ESCAPED_STRING) ("." (CNAME | ESCAPED_STRING))*

    value: SIGNED_NUMBER -> number
        | ESCAPED_STRING -> string
        | "Vero" -> true
        | "Falso" -> false
        | CNAME -> simple_key
        | QUALIFIED_CNAME-> simple_key
    
    // Atomo di base - può essere un valore o un'espressione tra parentesi
    atom: value
        | dictionary
        | pair
        | tuple
        | "(" or_expr ")"
    
    // Operatore NOT unario (massima precedenza tra operatori logici)
    not_expr: atom
        | "not" not_expr -> not_op
    
    // Esponenziazione (precedenza più alta tra operatori aritmetici)
    power_expr: not_expr
        | power_expr POW_OP not_expr -> power
    
    // Moltiplicazione, divisione, modulo
    mult_expr: power_expr
        | mult_expr MUL_OP power_expr -> multiply
        | mult_expr DIV_OP power_expr -> divide
        | mult_expr MOD_OP power_expr -> modulo
    
    // Addizione e sottrazione
    add_expr: mult_expr
        | add_expr ADD_OP mult_expr -> add
        | add_expr SUB_OP mult_expr -> subtract
    
    // Operatori di confronto
    comparison_expr: add_expr
        | comparison_expr "==" add_expr -> eq
        | comparison_expr "!=" add_expr -> neq
        | comparison_expr ">=" add_expr -> gte
        | comparison_expr "<=" add_expr -> lte
        | comparison_expr ">" add_expr -> gt
        | comparison_expr "<" add_expr -> lt
    
    // Operatore AND logico
    and_expr: comparison_expr
        | and_expr "and" comparison_expr -> and_op
    
    // Operatore OR logico (precedenza più bassa)
    or_expr: and_expr
        | or_expr "or" and_expr -> or_op
    
    // Case - ora usa or_expr invece di value
    case.8: or_expr -> valor
           | tuple -> valor
           
    
    // Dizionario
    dictionary.10: "{" (pair ";")* ";"?  "}" | (pair ";")*

    // Pair - La chiave deve essere un valore semplice, non un'espressione con operatori
    pair_statement.10: value ":" expression | tuple_inline ":" expression | value ":" tuple_inline
    pair: "(" pair_statement ")" | pair_statement
    
    // Case senza pair per evitare ambiguità in tuple_inline
    // Questo costringe a usare parentesi per tuple di pairs: (a:1, b:2) invece di a:1, b:2
    valid_tuple_item: value -> valor
                    | dictionary -> valor
                    | tuple -> valor
                    | "(" or_expr ")" -> valor

    // Tuple - Ora accetta espressioni
    tuple: "(" [ expression ("," expression)*] ")" -> tuple_
    // tuple_inline usa valid_tuple_item invece di case per evitare di matchare pairs
    tuple_inline: [valid_tuple_item "," valid_tuple_item ("," valid_tuple_item)*] -> tuple_

    // Espressione con pipeline
    expression: or_expr (PIPE (or_expr | tuple_inline))* -> expression

    // Importazione dei Token standard di Lark
    %import common.SIGNED_NUMBER
    %import common.ESCAPED_STRING
    %import common.CNAME
    %import common.LETTER
    %import common.WS

    // Ignora spazi bianchi e commenti
    %ignore WS
    %ignore COMMENT
"""

class DSLVariable:
    def __init__(self, name):
        self.name = name
    def __repr__(self):
        return f"VAR({self.name})"
    def __str__(self):
        return self.name

@v_args(inline=True)
class ConfigTransformer(Transformer):
    
    def start(self, *items):
        if not items:
            return {}
        return items[0]
        
    # --- Funzioni del Transformer ---

    def pair_statement(self, key, value):
        # Se la chiave è una variabile, usa il suo nome come chiave del dizionario
        if isinstance(key, DSLVariable):
            key = key.name
        return str(key), value

    def dictionary(self, *statements):
        return dict(statements)
    
    # --- Passthrough methods for expression hierarchy ---
    def atom(self, value):
        return value
    
    def not_expr(self, value):
        return value
    
    def power_expr(self, value):
        return value
    
    def mult_expr(self, value):
        return value
    
    def add_expr(self, value):
        return value
    
    def comparison_expr(self, value):
        return value
    
    def and_expr(self, value):
        return value
    
    def or_expr(self, value):
        return value
    
    # --- Operatori Aritmetici ---
    # Questi creano tuple che verranno valutate dal visitor dopo la risoluzione delle variabili
    # Nota: con @v_args(inline=True), i token degli operatori vengono passati come argomenti
    def add(self, left, op, right):
        return ('OP_ADD', left, right)
    
    def subtract(self, left, op, right):
        return ('OP_SUB', left, right)
    
    def multiply(self, left, op, right):
        return ('OP_MUL', left, right)
    
    def divide(self, left, op, right):
        return ('OP_DIV', left, right)
    
    def modulo(self, left, op, right):
        return ('OP_MOD', left, right)
    
    def power(self, left, op, right):
        return ('OP_POW', left, right)
    
    # --- Operatori di Confronto ---
    # Nota: COMPARISON_OP è un token con alternative, ma non viene passato come argomento separato
    def eq(self, left, right):
        return ('OP_EQ', left, right)
    
    def neq(self, left, right):
        return ('OP_NEQ', left, right)
    
    def gt(self, left, right):
        return ('OP_GT', left, right)
    
    def lt(self, left, right):
        return ('OP_LT', left, right)
    
    def gte(self, left, right):
        return ('OP_GTE', left, right)
    
    def lte(self, left, right):
        return ('OP_LTE', left, right)
    
    # --- Operatori Logici ---
    # Nota: 'and', 'or', 'not' sono keywords, non token, quindi non vengono passati come argomenti
    def and_op(self, left, right):
        return ('OP_AND', left, right)
    
    def or_op(self, left, right):
        return ('OP_OR', left, right)
    
    def not_op(self, expr):
        return ('OP_NOT', expr)

    def expression(self, *items):
        #print(f"{items} |EXPRESSION")
        
        # Filtra i token PIPE e mantiene solo gli operandi
        pipeline = []
        for item in items:
            if isinstance(item, Token) and item.type == 'PIPE':
                continue
            pipeline.append(item)
        
        # Se c'è un solo elemento, restituiscilo direttamente (appiattimento)
        if len(pipeline) == 1:
            return pipeline[0]
            
        # Restituisce una tupla identificativa e la lista di operazioni
        return ('EXPRESSION', pipeline)

    def tuple_(self, *items):
        #print(f"{items} |TUPLE", len(items))
        
        # 1. Filtra eventuali elementi None (da optional?)
        lista_filtrata = [elemento for elemento in items if elemento is not None]

        # 2. Scapsulamento se singolo elemento (permette raggruppamento (expr))
        if len(lista_filtrata) == 1:
            return lista_filtrata[0] 
        
        # 3. Altrimenti tupla
        return tuple(lista_filtrata)

    def inline_dict(self, key, value):
        #print(f"{key}: {value} |INLINE DICT")
        return str(key), value

    # --- Tipi Primitivi ---

    def number(self, n):
        n_str = str(n)
        return float(n_str) if '.' in n_str and 'E' not in n_str.upper() else int(n_str)

    def string(self, s):
        return str(s).strip('"')

    def true(self): return True
    def false(self): return False
    
    def simple_key(self, s):
        return DSLVariable(str(s))

    def valor(self, s):
        return s
    
    # --- Liste/Tuple ---

    def pair(self, *items):
        # Gestisce eventuali parentesi attorno a pair
        return items[0] if items else None
        
    # --- Strutture Complesse ---
    
    def pipeline(self, s):
        return str(s).strip()

class DSLVisitor:
    """
    Visitatore che attraversa il dizionario risultante dal parsing
    ed esegue le espressioni marcate come 'EXPRESSION'.
    Supporta funzioni definite in Python (functions_map) e nel DSL stesso.
    """
    def __init__(self, functions_map=None):
        self.functions_map = functions_map or {}
        self.root_data = {} # Contesto globale del DSL per lookup funzioni

    async def run(self, data):
        """Metodo di ingresso che imposta il contesto globale."""
        self.root_data = data
        return await self.visit(data)

    async def visit(self, node, local_context=None):
        """Visita un nodo, con supporto opzionale per contesto locale."""
        if isinstance(node, dict):
            # Visita ricorsiva per ogni valore del dizionario
            return {k: await self.visit(v, local_context) for k, v in node.items()}
        elif isinstance(node, list):
            # Visita ricorsiva per le liste
            return [await self.visit(x, local_context) for x in node]
        elif isinstance(node, tuple):
            # Controlla se è un'operazione da valutare
            if len(node) > 0 and isinstance(node[0], str) and node[0].startswith('OP_'):
                return await self.evaluate_operation(node, local_context)
            # Controlla se è un'espressione da eseguire
            elif len(node) > 0 and node[0] == 'EXPRESSION':
                return await self.evaluate_expression(node[1], local_context)
            else:
                # Heuristic per Function Definition: (inputs, {body}, outputs)
                # Se sembra una definizione di funzione, NON valutare il corpo (il dizionario centrale)
                # perché deve rimanere "code" (tuple di operazioni) per essere eseguito successivamente.
                if len(node) == 3 and isinstance(node[1], dict) and isinstance(node[0], (list, tuple)) and isinstance(node[2], (list, tuple)):
                     inputs = await self.visit(node[0], local_context)
                     # PRESERVA il body originale (un-evaluated)
                     body = node[1] 
                     outputs = await self.visit(node[2], local_context)
                     return (inputs, body, outputs)

                # Altrimenti visita gli elementi della tupla
                return tuple([await self.visit(x, local_context) for x in node])
        elif isinstance(node, DSLVariable):
            # 1. Lookup nel contesto locale (se fornito)
            if local_context and node.name in local_context:
                val = local_context[node.name]
                if val == node:
                    return node.name
                return await self.visit(val, local_context)
            
            # 2. Lookup nel contesto globale del DSL (root_data)
            if node.name in self.root_data:
                val = self.root_data[node.name]
                if val == node:
                     return node.name
                return await self.visit(val, local_context)
            
            # 3. Lookup nelle funzioni/variabili Python mappate
            if node.name in self.functions_map:
                return self.functions_map[node.name]
                
            return node.name
        else:
            return node
    
    async def evaluate_operation(self, op_tuple, local_context=None):
        """Valuta un'operazione aritmetica, logica o di confronto."""
        op_type = op_tuple[0]
        
        # Helper per risolvere un valore (potrebbe essere una variabile stringa, DSLVariable, o un valore diretto)
        async def resolve_value(val):
            # 1. Controlla nel contesto locale
            if local_context and isinstance(val, str) and val in local_context:
                return await self.visit(local_context[val], local_context)
            # 2. Controlla nel contesto globale
            elif isinstance(val, str) and val in self.root_data:
                return await self.visit(self.root_data[val], local_context)
            # 3. Altrimenti visitalo normalmente
            else:
                return await self.visit(val, local_context)
        
        # Operazioni unarie (NOT)
        if op_type == 'OP_NOT':
            operand = await resolve_value(op_tuple[1])
            return not operand
        
        # Operazioni binarie
        left = await resolve_value(op_tuple[1])
        right = await resolve_value(op_tuple[2])
        
        # Operatori aritmetici
        if op_type == 'OP_ADD':
            return left + right
        elif op_type == 'OP_SUB':
            return left - right
        elif op_type == 'OP_MUL':
            return left * right
        elif op_type == 'OP_DIV':
            return left / right
        elif op_type == 'OP_MOD':
            return left % right
        elif op_type == 'OP_POW':
            return left ** right
        
        # Operatori di confronto
        elif op_type == 'OP_EQ':
            return left == right
        elif op_type == 'OP_NEQ':
            return left != right
        elif op_type == 'OP_GT':
            return left > right
        elif op_type == 'OP_LT':
            return left < right
        elif op_type == 'OP_GTE':
            return left >= right
        elif op_type == 'OP_LTE':
            return left <= right
        
        # Operatori logici
        elif op_type == 'OP_AND':
            return left and right
        elif op_type == 'OP_OR':
            return left or right
        
        else:
            framework_log("ERROR", f"Operazione sconosciuta: {op_type}", emoji="❌")
            return None

    async def evaluate_expression(self, pipeline_ops, local_context=None):
        """
        Esegue la pipeline di operazioni.
        Esempio: [valore, func1, func2] -> func2(func1(valore))
        Supporta anche il formato tupla: [valore, (Inputs, Funzione, Outputs)]
        """
        result = None
        
        for i, op in enumerate(pipeline_ops):
            if i == 0:
                # Il primo elemento è il valore iniziale
                result = await self.visit(op, local_context) 
            else:
                # Caso 1: Firma esplicita ( (Inputs...), Funzione, (Outputs...) )
                if isinstance(op, (list, tuple)) and len(op) == 3:
                    inputs_def, func_name_node, outputs_def = op
                    func_name = str(func_name_node)
                    
                    # Risoluzione della funzione (Python o DSL)
                    func = self.functions_map.get(func_name)
                    
                    if func:
                        # Mappatura Input: result (precedente) + inputs_def
                        args = [result]
                        if isinstance(inputs_def, (list, tuple)):
                            for arg in inputs_def:
                                args.append(await self.visit(arg))
                        elif inputs_def is not None and inputs_def != ():
                            args.append(await self.visit(inputs_def))
                        
                        try:
                            if asyncio.iscoroutinefunction(func):
                                result = await func(*args)
                            else:
                                result = func(*args)
                        except Exception as e:
                            framework_log("ERROR", f"[{func_name}] Errore esecuzione Python: {e}", emoji="🐍")
                    
                    # TODO: Gestione Outputs se necessario (per ora result è già l'output)
                    
                # Caso 2: Funzioni/Operazioni semplici (stringa o variabile)
                else:
                    func_name = str(op)
                    
                    # 1. Cerca nelle funzioni Python mappate
                    if func_name in self.functions_map:
                        try:
                            func = self.functions_map[func_name]
                            if asyncio.iscoroutinefunction(func):
                                result = await func(result)
                            else:
                                result = func(result)
                        except Exception as e:
                            framework_log("ERROR", f"[{func_name}] Errore esecuzione Python: {e}", emoji="🐍")
                    
                    # 2. Cerca nelle funzioni definite nel DSL (root_data)
                    elif func_name in self.root_data:
                        dsl_def = self.root_data[func_name]
                        if isinstance(dsl_def, tuple) and len(dsl_def) == 3:
                            try:
                                result = await self.execute_dsl_function(dsl_def, result)
                            except Exception as e:
                                framework_log("ERROR", f"[{func_name}] Errore esecuzione DSL: {e}", emoji="📜")
                        else:
                             framework_log("WARNING", f"[{func_name}] Trovato nel DSL ma formato non valido per funzione: {type(dsl_def)}", emoji="⚠️")
                    
                    else:
                        framework_log("WARNING", f"[{func_name}] Funzione NON trovata (Python o DSL).", emoji="🤷")
                    
        return result

    async def execute_dsl_function(self, func_def, input_args):
        """
        Esegue una funzione definita nel DSL.
        Firma attesa: ( (Inputs...), { Body... }, (Outputs...) )
        """
        inputs_def, body_def, outputs_def = func_def
        
        # --- 1. Mappatura Input ---
        # inputs_def può essere una tupla di coppie (es: (integer:a, float:b)) o una singola coppia
        # input_args può essere un valore singolo o una tupla
        
        local_context = {}
        
        # Normalizzazione inputs_def in lista di (type, name) o (name)
        input_params = []
        
        # Helper per estrarre il nome parametro da una definizione (che può essere coppia 'tipo:nome' o solo 'nome')
        def extract_param_name(p):
            # Se è una coppia (es: ('integer', 'a')), il nome è 'a'
            if isinstance(p, tuple) and len(p) == 2:
                return str(p[1]) # Secondo elem è il nome
            return str(p)

        # Se inputs_def è una tupla che contiene stringhe E non è una coppia ('tipo', 'val')
        # Ma (tipo, val) è una tupla.
        # Caso singolo parametro: inputs_def = ('integer', 'a')
        # Caso multi parametro: inputs_def = ( ('integer', 'a'), ('float', 'b') )
        
        if isinstance(inputs_def, tuple) and len(inputs_def) == 2 and isinstance(inputs_def[0], str):
             # È una singola coppia ('tipo', 'nome') -> un solo parametro
             input_params.append(extract_param_name(inputs_def))
        elif isinstance(inputs_def, tuple):
             # È una tupla di parametri
             for p in inputs_def:
                 input_params.append(extract_param_name(p))
        else:
             # Fallback, magari è solo il nome 'a'
             input_params.append(str(inputs_def))
             
        # Mapping valori
        if len(input_params) == 1:
            # Un solo parametro riceve tutto l'input
            local_context[input_params[0]] = input_args
        else:
            # Più parametri: input_args deve essere iterabile (tupla/lista)
            if not isinstance(input_args, (list, tuple)):
                 framework_log("ERROR", f"Mismatch argomenti: Attesi {len(input_params)}, ricevuto singolo scalare: {input_args}", emoji="❌")
                 return None
            if len(input_args) != len(input_params):
                 framework_log("ERROR", f"Mismatch argomenti. Attesi {len(input_params)}, ricevuti {len(input_args)}", emoji="❌")
                 return None
            
            for name, val in zip(input_params, input_args):
                local_context[name] = val
                
        # --- 2. Esecuzione Body ---
        # body_def è un dizionario (es: {'output': ('OP_ADD', 'a', 'b')})
        
        for key, expr in body_def.items():
            try:
                # Usa visit() per valutare l'espressione, che gestirà sia operazioni che valori semplici
                # Passa local_context per permettere la risoluzione delle variabili locali
                val = await self.visit(expr, local_context)
                local_context[str(key)] = val
            except Exception as e:
                print(f"Errore valutazione espressione '{key}': {e}")
                
        # --- 3. Return Output ---
        # outputs_def definisce cosa ritornare (es: (float:output))
        # Estraiamo il nome della variabile da ritornare
        ret_val = None
        
        output_vars = []
        if isinstance(outputs_def, tuple) and len(outputs_def) == 2 and isinstance(outputs_def[0], str):
             output_vars.append(extract_param_name(outputs_def))
        elif isinstance(outputs_def, tuple):
             for p in outputs_def:
                 output_vars.append(extract_param_name(p))
        else:
             output_vars.append(str(outputs_def))
             
        if len(output_vars) == 1:
            var_name = output_vars[0]
            ret_val = local_context.get(var_name, None)
        else:
            ret_val = tuple(local_context.get(v, None) for v in output_vars)
            
        return ret_val

# ----------------------------------------------------------------------
# --- 3. Funzione Principale di Parsing ---
# ----------------------------------------------------------------------

def parse_dsl_file(content):
    parser = Lark(grammar, parser='earley')
    tree = parser.parse(content)
    return ConfigTransformer().transform(tree)

async def execute_dsl_file(content):
    config = parse_dsl_file(content)
    visitor = DSLVisitor(functions_map=dsl_functions)
    
    # Nota: Usiamo visitor.run() invece di visit() per inizializzare il contesto
    final_result = await visitor.run(config)
    return final_result

async def run_dsl_tests(visitor: DSLVisitor, parsed_data: dict, functions_map: dict):
    """
    Esegue tutti i casi definiti nella sezione 'test_suite' del DSL.
    """
    test_suite = parsed_data.get('test_suite')
    
    if not test_suite or not isinstance(test_suite, tuple):
        print("🔴 Errore: Sezione 'test_suite' non trovata o non valida nel file DSL.")
        return False

    all_passed = True
    print("\n====================================")
    print(f"Esecuzione {len(test_suite)} Casi di Test DSL")
    print("====================================")

    for test_case in test_suite:
        if not isinstance(test_case, dict):
            print(f"🔴 Errore nel formato del caso test: {test_case}")
            continue

        test_id = test_case.get('id', 'N/A')
        target_name = test_case.get('target')
        input_args = test_case.get('input_args')
        expected = test_case.get('expected_output')

        print(f"\n[Test ID: {test_id}] Testing '{target_name}'...")

        try:
            # 1. Trova la funzione/pipeline target nel contesto globale (root_data)
            target_def = parsed_data.get(target_name)

            if target_def is None:
                print(f"🔴 FALLITO: Target '{target_name}' non trovato nel DSL.")
                all_passed = False
                continue

            # 2. Esecuzione: Le funzioni DSL sono tuple di 3 elementi (Args, Body, Ret)
            if isinstance(target_def, tuple) and len(target_def) == 3:
                # Esegui la funzione DSL
                actual_output = await visitor.execute_dsl_function(target_def, input_args)
            
            # 3. Esecuzione: Se è una Pipeline (già pre-valutata come 'EXPRESSION' dal Transformer, ma che deve essere eseguita qui)
            elif isinstance(target_def, tuple) and len(target_def) > 0 and target_def[0] == 'EXPRESSION':
                 # Per testare una pipeline che non è una funzione, dobbiamo iniettare
                 # l'input_args nel primo elemento della pipeline e rieseguire.
                 # Questo è complesso, quindi è meglio definire le pipeline come funzioni DSL
                 # che accettano l'input, come fatto nell'esempio di prima.
                 # Per semplicità, qui assumiamo che solo le funzioni DSL siano testate direttamente.
                 print(f"⚠️ Tipo target non supportato per test diretto: {target_name}. Usare una funzione DSL.")
                 all_passed = False
                 continue
                 
            else:
                 # Se il target non è una funzione DSL, eseguiamo la sua valutazione.
                 # Ad esempio, se target è 'numeri: 100;' e lo vogliamo testare
                 actual_output = await visitor.visit(target_def)


            # 4. Confronto
            if actual_output == expected:
                print("🟢 PASSATO!")
            else:
                print("🔴 FALLITO!")
                print(f"   Atteso: {expected}")
                print(f"   Ottenuto: {actual_output}")
                all_passed = False

        except Exception as e:
            print(f"🔴 FALLITO con ECCEZIONE: {e}")
            all_passed = False
            
    print("\n====================================")
    print(f"RISULTATO FINALE: {'🟢 TUTTI I TEST PASSATI' if all_passed else '🔴 TEST FALLITI'}")
    print("====================================")
    return all_passed

# ----------------------------------------------------------------------
# --- 4. Esempio di Utilizzo ---
# ----------------------------------------------------------------------

# --- Funzioni definite nel codice Python ---
def custom_print(data):
    print(f"*** CUSTOM PRINT ***: {data}")
    return data 

async def dsl_resource(path):
    """Carica una risorsa (JSON o modulo Python) e ne valida il contratto."""
    from framework.service.load import resource
    try:
        if isinstance(path, dict) and 'path' in path:
            path = path['path']
        
        res = await resource(path=path)
        if isinstance(res, dict) and 'data' in res:
             return res['data']
        return res
    except Exception as e:
        framework_log("ERROR", f"Errore dsl_resource per {path}: {e}", emoji="❌")
        return None

async def dsl_storekeeper(*args,**kargs):
    """Carica una risorsa (JSON o modulo Python) e ne valida il contratto."""
    from framework.service.load import storekeeper
    try:
        res = await storekeeper(*args,**kargs)
        if isinstance(res, dict) and 'data' in res:
             return res['data']
        return res
    except Exception as e:
        framework_log("ERROR", f"Errore dsl_storekeeper: {e}", emoji="❌")
        return None

async def dsl_messenger(*args,**kargs):
    """Carica una risorsa (JSON o modulo Python) e ne valida il contratto."""
    from framework.service.load import messenger
    try:
        res = await messenger(*args,**kargs)
        if isinstance(res, dict) and 'data' in res:
             return res['data']
        return res
    except Exception as e:
        framework_log("ERROR", f"Errore dsl_messenger: {e}", emoji="❌")
        return None

async def dsl_executor(*args,**kargs):
    """Carica una risorsa (JSON o modulo Python) e ne valida il contratto."""
    from framework.service.load import executor
    try:
        res = await executor(*args,**kargs)
        if isinstance(res, dict) and 'data' in res:
             return res['data']
        return res
    except Exception as e:
        framework_log("ERROR", f"Errore dsl_executor: {e}", emoji="❌")
        return None

async def dsl_presenter(*args,**kargs):
    """Carica una risorsa (JSON o modulo Python) e ne valida il contratto."""
    from framework.service.load import presenter
    try:
        res = await presenter(*args,**kargs)
        if isinstance(res, dict) and 'data' in res:
             return res['data']
        return res
    except Exception as e:
        framework_log("ERROR", f"Errore dsl_presenter: {e}", emoji="❌")
        return None

async def dsl_defender(*args,**kargs):
    """Carica una risorsa (JSON o modulo Python) e ne valida il contratto."""
    from framework.service.load import defender
    try:
        res = await defender(*args,**kargs)
        if isinstance(res, dict) and 'data' in res:
             return res['data']
        return res
    except Exception as e:
        framework_log("ERROR", f"Errore dsl_defender: {e}", emoji="❌")
        return None

# Mappa delle funzioni disponibili per le espressioni DSL
dsl_functions = {
    'storekeeper': dsl_storekeeper,
    'messenger': dsl_messenger,
    'executor': dsl_executor,
    'presenter': dsl_presenter,
    'defender': dsl_defender,
    'resource': dsl_resource,
    'format': flow.format,
    'foreach': flow.foreach,
    'convert': flow.convert,
    'print': custom_print,
    'get': flow.get,
    'map': lambda d, f: [mistql.query(f, data=i) for i in d] if isinstance(d, list) else d,
    'merge': lambda a, b: (a | b) if isinstance(a, dict) and isinstance(b, dict) else b,
    'query': lambda data, q: mistql.query(q, data=data),
    # Tipi base per conversioni
    'dict': dict,
    'list': list,
    'str': str,
    'int': int,
    'float': float,
    'relative': int,
    'natural': int,
    'boolean': bool,
    'rational': float,
    'complex': float,
}
