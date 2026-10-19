import os
import time
import threading
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
origins = [x.strip() for x in os.getenv('CORS_ORIGINS', 'http://localhost:62328').split(',') if x.strip()]
CORS(app, origins=origins)
product_lock = threading.Lock()

@contextmanager
def connection():
    url = os.environ['DATABASE_URL']
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=10) as conn:
        yield conn

def serialize(row):
    if row is None:
        return None
    obj = dict(row)
    obj['price'] = float(obj['price'])
    return obj

def payload(require_version=False):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('Se requiere un objeto JSON')
    fields = ['names', 'price', 'stock']
    if request.method == 'PUT':
        fields.append('id')
    if require_version:
        fields.append('version')
    missing = [f for f in fields if f not in data]
    if missing:
        raise ValueError('Faltan campos: ' + ', '.join(missing))
    if not isinstance(data['names'], str) or not data['names'].strip() or len(data['names']) > 100:
        raise ValueError('names debe tener entre 1 y 100 caracteres')
    try:
        price = Decimal(str(data['price']))
        if not price.is_finite() or price < 0 or price >= 100000000:
            raise ValueError()
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('price debe ser un número válido no negativo (máximo 99999999.99)')
    if isinstance(data['stock'], bool) or not isinstance(data['stock'], int) or data['stock'] < 0:
        raise ValueError('stock debe ser un entero no negativo')
    for field in (['id', 'version'] if require_version else ['id'] if request.method == 'PUT' else []):
        if isinstance(data[field], bool) or not isinstance(data[field], int) or data[field] < 1:
            raise ValueError(f'{field} debe ser un entero positivo')
    return data, price

@app.errorhandler(ValueError)
def invalid(error):
    return jsonify(mensaje=str(error)), 400

@app.get('/')
def home():
    return jsonify(mensaje='API de productos activa')

@app.get('/test-db')
def test_db():
    with connection() as conn:
        row = conn.execute('SELECT current_database() AS database').fetchone()
    return jsonify(mensaje='Conexion exitosa a la base de datos', database=row['database'])

@app.get('/api/products')
def get_products():
    with connection() as conn:
        rows = conn.execute('SELECT id, names, price, stock, version FROM public.products_vw ORDER BY id').fetchall()
    return jsonify([serialize(row) for row in rows])

@app.get('/api/products/<int:product_id>')
def get_product(product_id):
    with connection() as conn:
        row = conn.execute('SELECT id, names, price, stock, version FROM public.products_vw WHERE id = %s', (product_id,)).fetchone()
    if row is None:
        return jsonify(mensaje='Producto no encontrado'), 404
    return jsonify(serialize(row))

@app.post('/api/products')
def create_product():
    data, price = payload()
    with connection() as conn:
        row = conn.execute('INSERT INTO public.products (names, price, stock) VALUES (%s, %s, %s) RETURNING id, names, price, stock, version', (data['names'].strip(), price, data['stock'])).fetchone()
    return jsonify(mensaje='Producto creado correctamente', producto=serialize(row)), 201

def update_common(use_lock=False, use_version=False):
    data, price = payload(require_version=use_version)
    def execute_update():
        with connection() as conn:
            if use_version:
                row = conn.execute('''UPDATE public.products SET names=%s, price=%s, stock=%s, version=version+1 WHERE id=%s AND version=%s RETURNING id, names, price, stock, version''', (data['names'].strip(), price, data['stock'], data['id'], data['version'])).fetchone()
            else:
                row = conn.execute('''UPDATE public.products SET names=%s, price=%s, stock=%s, version=version+1 WHERE id=%s RETURNING id, names, price, stock, version''', (data['names'].strip(), price, data['stock'], data['id'])).fetchone()
            if row is not None:
                return jsonify(mensaje='Producto actualizado correctamente', producto=serialize(row)), 200
            current = conn.execute('SELECT id, names, price, stock, version FROM public.products WHERE id=%s', (data['id'],)).fetchone()
            if current is None:
                return jsonify(mensaje='Producto no encontrado'), 404
            return jsonify(error='Conflicto de concurrencia', mensaje='Otro usuario modificó el producto antes que usted', producto_actual=serialize(current)), 409
    if use_lock:
        with product_lock:
            return execute_update()
    return execute_update()

@app.put('/api/products')
def update_product():
    result = update_common()
    if os.getenv('SIMULATE_DELAY_SECONDS', '0') != '0':
        time.sleep(max(0, min(float(os.environ['SIMULATE_DELAY_SECONDS']), 30)))
    return result

@app.put('/api/products/Lock')
def update_lock():
    return update_common(use_lock=True)

@app.put('/api/products/version')
def update_version():
    return update_common(use_version=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
