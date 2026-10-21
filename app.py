import os
from decimal import Decimal, InvalidOperation

import hmac
import html
import re
import resend
import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
origins = [x.strip() for x in os.getenv('CORS_ORIGINS', 'http://localhost:62328').split(',') if x.strip()]
CORS(app, origins=origins)


class Database:
    @staticmethod
    def get_connection():
        return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10, row_factory=dict_row)


def product_dict(row):
    return {
        'id': row['id'],
        'names': row['names'],
        'price': float(row['price']),
        'stock': row['stock'],
        'version': row['version'],
    }


def validate_product(data, require_id=False, require_version=False):
    if not isinstance(data, dict):
        raise ValueError('Debes enviar un objeto JSON')
    required = ['names', 'price', 'stock']
    if require_id:
        required.append('id')
    if require_version:
        required.append('version')
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError('Faltan campos: ' + ', '.join(missing))
    names = data['names']
    if not isinstance(names, str) or not names.strip() or len(names) > 100:
        raise ValueError('names debe ser un texto de 1 a 100 caracteres')
    try:
        price = Decimal(str(data['price']))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('price debe ser un número válido')
    if not price.is_finite() or price < 0 or price > Decimal('99999999.99') or price.as_tuple().exponent < -2:
        raise ValueError('price debe estar entre 0 y 99999999.99, con máximo 2 decimales')
    stock = data['stock']
    if type(stock) is not int or stock < 0 or stock > 2147483647:
        raise ValueError('stock debe ser un entero no negativo')
    result = {'names': names.strip(), 'price': price, 'stock': stock}
    for key in (['id'] if require_id else []) + (['version'] if require_version else []):
        if type(data[key]) is not int or data[key] < 1:
            raise ValueError(f'{key} debe ser un entero positivo')
        result[key] = data[key]
    return result


def error_response(exc):
    app.logger.exception('Error de base de datos o servidor')
    return jsonify({'mensaje': 'Error interno del servidor'}), 500


@app.get('/')
def home():
    return jsonify({'mensaje': 'API de productos activa'})


@app.get('/test-db')
def test_db():
    try:
        with Database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT current_database() AS database')
                row = cur.fetchone()
        return jsonify({'mensaje': 'Conexion exitosa a la base de datos', 'database': row['database']})
    except Exception as exc:
        return error_response(exc)


@app.get('/api/products')
def get_products():
    try:
        with Database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id, names, price, stock, version FROM public.products_vw ORDER BY id')
                rows = cur.fetchall()
        return jsonify([product_dict(row) for row in rows])
    except Exception as exc:
        return error_response(exc)


@app.get('/api/products/<int:product_id>')
def get_product(product_id):
    try:
        with Database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id, names, price, stock, version FROM public.products_vw WHERE id = %s', (product_id,))
                row = cur.fetchone()
        if row is None:
            return jsonify({'mensaje': 'Producto no encontrado'}), 404
        return jsonify(product_dict(row))
    except Exception as exc:
        return error_response(exc)


@app.post('/api/products')
def create_product():
    try:
        data = validate_product(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({'mensaje': str(exc)}), 400
    try:
        with Database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO public.products (names, price, stock) VALUES (%s, %s, %s) RETURNING id, names, price, stock, version',
                            (data['names'], data['price'], data['stock']))
                row = cur.fetchone()
        return jsonify({'mensaje': 'Producto creado correctamente', 'producto': product_dict(row)}), 201
    except Exception as exc:
        return error_response(exc)


@app.put('/api/products')
def update_product():
    try:
        data = validate_product(request.get_json(silent=True), require_id=True)
    except ValueError as exc:
        return jsonify({'mensaje': str(exc)}), 400
    try:
        with Database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''UPDATE public.products SET names=%s, price=%s, stock=%s, version=version+1
                               WHERE id=%s RETURNING id, names, price, stock, version''',
                            (data['names'], data['price'], data['stock'], data['id']))
                row = cur.fetchone()
        if row is None:
            return jsonify({'mensaje': 'Producto no encontrado'}), 404
        return jsonify({'mensaje': 'Producto actualizado correctamente', 'producto': product_dict(row)})
    except Exception as exc:
        return error_response(exc)


@app.put('/api/products/Lock')
def update_lock():
    try:
        data = validate_product(request.get_json(silent=True), require_id=True)
    except ValueError as exc:
        return jsonify({'mensaje': str(exc)}), 400
    try:
        with Database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM public.products WHERE id=%s FOR UPDATE', (data['id'],))
                if cur.fetchone() is None:
                    return jsonify({'mensaje': 'Producto no encontrado'}), 404
                cur.execute('''UPDATE public.products SET names=%s, price=%s, stock=%s, version=version+1
                               WHERE id=%s RETURNING id, names, price, stock, version''',
                            (data['names'], data['price'], data['stock'], data['id']))
                row = cur.fetchone()
        return jsonify({'mensaje': 'Producto actualizado con bloqueo', 'producto': product_dict(row)})
    except Exception as exc:
        return error_response(exc)


@app.put('/api/products/version')
def update_version():
    try:
        data = validate_product(request.get_json(silent=True), require_id=True, require_version=True)
    except ValueError as exc:
        return jsonify({'mensaje': str(exc)}), 400
    try:
        with Database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''UPDATE public.products SET names=%s, price=%s, stock=%s, version=version+1
                               WHERE id=%s AND version=%s RETURNING id, names, price, stock, version''',
                            (data['names'], data['price'], data['stock'], data['id'], data['version']))
                row = cur.fetchone()
                if row is None:
                    cur.execute('SELECT id, names, price, stock, version FROM public.products WHERE id=%s', (data['id'],))
                    current = cur.fetchone()
                    if current is None:
                        return jsonify({'mensaje': 'Producto no encontrado'}), 404
                    return jsonify({'error': 'Conflicto de concurrencia',
                                    'mensaje': 'Otro usuario modificó el producto antes que usted',
                                    'producto_actual': product_dict(current)}), 409
        return jsonify({'mensaje': 'Producto actualizado con versionamiento', 'producto': product_dict(row)})
    except Exception as exc:
        return error_response(exc)


@app.post('/api/send-products-email')
def send_products_email():

    # 1. Autenticación mediante API Key
    expected_key = os.getenv('API_KEY')
    received_key = request.headers.get('x-api-key', '')

    app.logger.warning(
    "AUTH DEBUG: configurada=%s, longitud_esperada=%s, "
    "longitud_recibida=%s, coincide=%s",
    bool(expected_key),
    len(expected_key or ''),
    len(received_key or ''),
    hmac.compare_digest(received_key, expected_key or '')
)

    if not expected_key:
        return jsonify({
            'success': False,
            'mensaje': 'API Key no configurada'
        }), 500

    if not hmac.compare_digest(received_key, expected_key):
        return jsonify({
            'success': False,
            'mensaje': 'No autorizado'
        }), 401

    # 2. Validar destinatario
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            'success': False,
            'mensaje': 'Debes enviar un objeto JSON'
        }), 400

    email = data.get('email', '')

    if (
        not isinstance(email, str)
        or len(email) > 254
        or not re.fullmatch(
            r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}',
            email
        )
    ):
        return jsonify({
            'success': False,
            'mensaje': 'Correo electrónico inválido'
        }), 400

    # 3. Consultar productos directamente desde Supabase
    try:
        with Database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    SELECT id, names, price, stock, version
                    FROM public.products_vw
                    ORDER BY id
                    '''
                )
                products = cur.fetchall()

    except Exception:
        app.logger.exception('Error consultando productos')
        return jsonify({
            'success': False,
            'mensaje': 'No se pudieron consultar los productos'
        }), 500

    if not products:
        return jsonify({
            'success': False,
            'mensaje': 'No existen productos disponibles'
        }), 404

    # 4. Preparar el catálogo en HTML
    rows = ''

    for product in products:
        name = html.escape(str(product['names']))
        price = float(product['price'])
        stock = html.escape(str(product['stock']))

        rows += f'''
        <tr>
            <td style="padding:10px;border:1px solid #ddd;">
                {name}
            </td>
            <td style="padding:10px;border:1px solid #ddd;">
                ${price:.2f}
            </td>
            <td style="padding:10px;border:1px solid #ddd;">
                {stock}
            </td>
        </tr>
        '''

    email_html = f'''
    <div style="font-family:Arial,sans-serif;max-width:650px;margin:auto;">
        <h1>Catálogo de Productos</h1>
        <p>Estos son nuestros productos disponibles:</p>

        <table style="width:100%;border-collapse:collapse;">
            <thead>
                <tr style="background:#f1f1f1;">
                    <th style="padding:10px;border:1px solid #ddd;">
                        Producto
                    </th>
                    <th style="padding:10px;border:1px solid #ddd;">
                        Precio
                    </th>
                    <th style="padding:10px;border:1px solid #ddd;">
                        Stock
                    </th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>

        <p>Gracias por consultar nuestro catálogo.</p>
    </div>
    '''

    # 5. Enviar correo utilizando Resend
    resend_key = os.getenv('RESEND_API_KEY')
    sender = os.getenv('MAIL_RESEND')

    if not resend_key or not sender:
        return jsonify({
            'success': False,
            'mensaje': 'Servicio de correo no configurado'
        }), 500

    try:
        resend.api_key = resend_key

        result = resend.Emails.send({
            'from': sender,
            'to': [email],
            'subject': 'Catálogo de Productos',
            'html': email_html
        })

        return jsonify({
            'success': True,
            'mensaje': 'Correo aceptado para envío',
            'resultado': str(result)
        }), 200

    except Exception:
        app.logger.exception('Error enviando correo')
        return jsonify({
            'success': False,
            'mensaje': 'No se pudo enviar el correo'
        }), 502



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
