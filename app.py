from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
import oracledb
import os
import time
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Importar SDK nativo de Azure
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

# ─────────────────────────────────────────────────────────────────
# 🛠️ PARCHE DE INYECCIÓN MANUAL PARA EL ARCHIVO .ENV
# ─────────────────────────────────────────────────────────────────
base_dir = os.path.dirname(__file__)
ruta_env = os.path.join(base_dir, '.env')
load_dotenv(dotenv_path=ruta_env)

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'mi_clave_secreta_para_sesiones_locales')

# =====================================================================
# ☁️ CONFIGURACIÓN DE AZURE BLOB STORAGE (MANUAL Y BLINDADA)
# =====================================================================
AZURE_NAME = os.getenv('AZURE_ACCOUNT_NAME', '')
AZURE_KEY = os.getenv('AZURE_ACCOUNT_KEY', '')
AZURE_CONTAINER = os.getenv('AZURE_CONTAINER', 'imagenes')

# RESPALDO DIRECTO: Si el .env no inyecta variables a la terminal, forzamos tus datos reales
if not AZURE_NAME or AZURE_NAME == '':
    AZURE_NAME = "stockpyme"
    AZURE_KEY = "EC4OScT+BRSPMr/mu1Tbg/gh0e3DQ6FQwcQ5gR7ztRBeG5U7q0ToW8DXaSAvg=="
    AZURE_CONTAINER = "imagenes"

# INSTANCIACIÓN ASEGURADA: Se ejecuta sí o sí con datos válidos
CADENA_CONEXION = f"DefaultEndpointsProtocol=https;AccountName={AZURE_NAME};AccountKey={AZURE_KEY};EndpointSuffix=core.windows.net"
blob_service_client = BlobServiceClient.from_connection_string(CADENA_CONEXION)
container_client = blob_service_client.get_container_client(AZURE_CONTAINER)

EXTENSIONES_PERMITIDAS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

def archivo_permitido(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in EXTENSIONES_PERMITIDAS

def generar_url_sas(blob_name):
    """Genera una URL firmada temporal de 1 hora para que el navegador pueda renderizar la foto de Azure"""
    try:
        sas_token = generate_blob_sas(
            account_name=AZURE_NAME,
            container_name=AZURE_CONTAINER,
            blob_name=blob_name,
            account_key=AZURE_KEY,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=1)
        )
        return f"https://{AZURE_NAME}.blob.core.windows.net/{AZURE_CONTAINER}/{blob_name}?{sas_token}"
    except Exception:
        return ''


# =====================================================================
# 🗄️ CONFIGURACIÓN DE CONEXIÓN ORACLE (Modo Thin Directo con Wallet)
# =====================================================================
DB_USER = os.getenv('DB_USER', 'ADMIN')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'Randy-2026**')  # Tu contraseña asignada por defecto si falla el env
DB_DSN = os.getenv('DB_DSN', 'miinventariofacil_medium')
RUTA_WALLET = os.getenv('RUTA_WALLET', r"C:\Users\fribe\OneDrive\Escritorio\StockPyme\wallet")
WALLET_PASSWORD = os.getenv('WALLET_PASSWORD', 'Randy-2026**')

def obtener_conexion():
    """Establece conexión directa en modo Thin pasando la clave del Wallet automáticamente"""
    return oracledb.connect(
        user=DB_USER,
        password=DB_PASSWORD,
        dsn=DB_DSN,
        config_dir=RUTA_WALLET,
        wallet_location=RUTA_WALLET,
        wallet_password=WALLET_PASSWORD
    )


# --- RUTAS DE NAVEGACIÓN ---

@app.route('/')
def inicio():
    if 'usuario_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'usuario_id' not in session:
        return redirect(url_for('inicio'))
    return render_template('dashboard.html')

@app.route('/logout')
def logout():
    session.pop('usuario_id', None)
    return redirect(url_for('inicio'))


# --- APIS CONECTADAS A ORACLE CLOUD + AZURE ---

@app.route('/api/login', methods=['POST'])
def api_login():
    datos = request.json
    correo = datos.get('correo').strip()
    contra = datos.get('contrasenia')
    
    try:
        conn = obtener_conexion()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM usuarios WHERE email = :1 AND contrasenia = :2", (correo, contra))
        usuario = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if usuario:
            session['usuario_id'] = usuario[0]
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "message": "Usuario o contraseña incorrectos"}), 401
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/productos', methods=['GET'])
def obtener_productos():
    if 'usuario_id' not in session:
        return jsonify({"status": "error", "message": "No autorizado"}), 401
        
    try:
        conn = obtener_conexion()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, nombre, categoria, cantidad, precio, imagen_nombre FROM productos WHERE usuario_id = :1 ORDER BY nombre ASC",
            (session['usuario_id'],)
        )
        
        columnas = [col[0].lower() for col in cursor.description]
        productos_raw = [dict(zip(columnas, fila)) for fila in cursor.fetchall()]
        
        cursor.close()
        conn.close()
        
        # Mapeamos los nombres de los blobs para transformarlos en URLs firmadas de Azure
        for prod in productos_raw:
            if prod.get('imagen_nombre'):
                prod['imagen_url'] = generar_url_sas(prod['imagen_nombre'])
            else:
                prod['imagen_url'] = ''
                
        return jsonify(productos_raw)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error al leer BD: {str(e)}"}), 500


@app.route('/api/productos', methods=['POST'])
def agregar_producto():
    if 'usuario_id' not in session:
        return jsonify({"status": "error", "message": "No autorizado"}), 401
        
    # Leer datos del formulario (FormData)
    nombre = request.form.get('nombre', '').strip()
    categoria = request.form.get('categoria', '').strip() or "Sin categoría"
    
    try:
        cantidad = int(request.form.get('cantidad', 0))
        precio = float(request.form.get('precio', 0.0))
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Datos numéricos inválidos"}), 400
    
    # Validación del archivo binario
    if 'imagen' not in request.files:
        return jsonify({"status": "error", "message": "La imagen es obligatoria"}), 400
        
    archivo = request.files['imagen']
    if archivo.filename == '':
        return jsonify({"status": "error", "message": "No has seleccionado ningún archivo"}), 400
        
    if not (archivo and archivo_permitido(archivo.filename)):
        return jsonify({"status": "error", "message": "Formato inválido. Usa png, jpg, jpeg o webp"}), 400

    try:
        # Generar nombre único del archivo para Azure
        nombre_limpio = secure_filename(archivo.filename)
        nombre_final_imagen = f"{int(time.time())}_{nombre_limpio}"
        
        # ─── 1. SUBIR EL ARCHIVO BINARIO DIRECTO A AZURE BLOB STORAGE ───
        blob_client = container_client.get_blob_client(nombre_final_imagen)
        blob_client.upload_blob(archivo.read(), blob_type="BlockBlob", overwrite=True)

        # ─── 2. GUARDAR LOS REGISTROS EN TU TABLA DE ORACLE CLOUD ───
        conn = obtener_conexion()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT id, cantidad FROM productos WHERE usuario_id = :1 AND LOWER(nombre) = LOWER(:2)",
            (session['usuario_id'], nombre)
        )
        existente = cursor.fetchone()
      
        if existente:
            id_producto = existente[0]
            nueva_cantidad = existente[1] + cantidad
            cursor.execute(
                "UPDATE productos SET cantidad = :1, precio = :2, categoria = :3, imagen_nombre = :4 WHERE id = :5",
                (nueva_cantidad, precio, categoria, nombre_final_imagen, id_producto)
            )
        else:
            cursor.execute(
                "INSERT INTO productos (nombre, categoria, cantidad, precio, usuario_id, imagen_nombre) VALUES (:1, :2, :3, :4, :5, :6)",
                (nombre, categoria, cantidad, precio, session['usuario_id'], nombre_final_imagen)
            )
    
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "success", "message": "Operación exitosa"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error en la operación de guardado: {str(e)}"}), 500


@app.route('/api/productos/eliminar/<int:producto_id>', methods=['DELETE'])
def eliminar_producto(producto_id):
    if 'usuario_id' not in session:
        return jsonify({"status": "error", "message": "No autorizado"}), 401
        
    try:
        conn = obtener_conexion()
        cursor = conn.cursor()
        
        # Eliminar también el archivo binario de Azure Blob Storage antes de borrar la fila
        cursor.execute("SELECT imagen_nombre FROM productos WHERE id = :1 AND usuario_id = :2", (producto_id, session['usuario_id']))
        producto = cursor.fetchone()
        
        if producto and producto[0]:
            try:
                blob_client = container_client.get_blob_client(producto[0])
                blob_client.delete_blob()
            except Exception:
                pass 
        
        cursor.execute("DELETE FROM productos WHERE id = :1 AND usuario_id = :2", (producto_id, session['usuario_id']))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error al eliminar: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)