from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from extensions import db, login_manager, bcrypt
from models import User, Product, Transaction, Supplier
from functools import wraps
import os
import pandas as pd
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import random
import string

# Load environment variables
load_dotenv()

app = Flask(__name__, static_folder='static')

# Security configurations
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
app.config['SESSION_COOKIE_SECURE'] = True  # HTTPS only
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Prevent XSS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # CSRF protection
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  # Session timeout

# PostgreSQL Configuration
database_url = os.environ.get('DATABASE_URL')
# Fix for Heroku/Render postgres:// to postgresql://
# if database_url and database_url.startswith('postgres://'):
#     database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,  # Verify connections before using
}

app.config['ALLOWED_EXTENSIONS'] = {'xlsx', 'xls'}
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024  # 4MB for Render limits

# Initialize extensions
db.init_app(app)
login_manager.init_app(app)
bcrypt.init_app(app)

# Configure login manager
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

# Force HTTPS in production
@app.before_request
def force_https():
    if not request.is_secure and request.headers.get('X-Forwarded-Proto') != 'https':
        # Allow localhost for development
        if 'localhost' not in request.host and '127.0.0.1' not in request.host:
            url = request.url.replace('http://', 'https://', 1)
            return redirect(url, code=301)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def generate_sku(prefix='PROD'):
    """Generate a unique SKU"""
    max_attempts = 100
    for _ in range(max_attempts):
        random_part = ''.join(random.choices(string.digits, k=6))
        sku = f"{prefix}{random_part}"
        
        if not Product.query.filter_by(sku=sku).first():
            return sku
    
    # Fallback if unlikely collision happens
    import uuid
    return f"{prefix}{str(uuid.uuid4())[:8].upper()}"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Health check endpoint for monitoring
@app.route('/health')
def health_check():
    """Lightweight health check for uptime monitors"""
    try:
        # Quick DB check
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'status': 'healthy', 'timestamp': datetime.now(timezone.utc).isoformat()}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

@app.route('/ping')
def ping():
    """Simple ping endpoint"""
    return 'pong', 200

# Routes
@app.route('/')
def index():
    """Root route - redirect to dashboard if logged in, otherwise to login"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('Please provide both username and password', 'danger')
            return render_template('login.html')
        
        user = User.query.filter_by(username=username).first()
        
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            user.last_login = datetime.now(timezone.utc)
            db.session.commit()
            next_page = request.args.get('next')
            flash('Logged in successfully!', 'success')
            return redirect(next_page if next_page else url_for('dashboard'))
        else:
            flash('Login failed. Please check username and password', 'danger')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
@login_required
def register():
    # Check if current user is admin
    if current_user.role != 'admin':
        flash('Access denied. Admin privileges required.', 'danger')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')
        
        # Validation
        if not username or not password:
            flash('Username and password are required', 'danger')
            return redirect(url_for('register'))
        
        if len(password) < 6:
            flash('Password must be at least 6 characters', 'danger')
            return redirect(url_for('register'))
        
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))
        
        if email and User.query.filter_by(email=email).first():
            flash('Email already exists', 'danger')
            return redirect(url_for('register'))
            
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(username=username, email=email, password=hashed_password, role=role)
        
        try:
            db.session.add(user)
            db.session.commit()
            flash(f'Account created successfully for {username}!', 'success')
            return redirect(url_for('register'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating account: {str(e)}', 'danger')
    
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out successfully!', 'success')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    # Calculate dashboard metrics with error handling
    try:
        total_products = Product.query.count()
        low_stock_products = Product.query.filter(Product.quantity <= Product.reorder_level).count()
        
        # Calculate total inventory value (more efficient query)
        inventory_stats = db.session.query(
            func.sum(Product.quantity * Product.unit_price).label('total_value'),
            func.sum(Product.quantity * Product.cost_price).label('total_cost')
        ).first()
        
        total_value = inventory_stats.total_value or 0
        total_cost = inventory_stats.total_cost or 0
        
        # Get recent transactions
        recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()
        
        # Get low stock alerts
        low_stock_items = Product.query.filter(
            Product.quantity <= Product.reorder_level
        ).order_by(Product.quantity.asc()).limit(20).all()
        
        return render_template('dashboard.html',
                             total_products=total_products,
                             low_stock_products=low_stock_products,
                             total_value=total_value,
                             total_cost=total_cost,
                             recent_transactions=recent_transactions,
                             low_stock_items=low_stock_items)
    except Exception as e:
        flash(f'Error loading dashboard: {str(e)}', 'danger')
        return render_template('dashboard.html',
                             total_products=0,
                             low_stock_products=0,
                             total_value=0,
                             total_cost=0,
                             recent_transactions=[],
                             low_stock_items=[])

@app.route('/products')
@login_required
def products():
    search = request.args.get('search', '').strip()
    category = request.args.get('category', '').strip()
    
    query = Product.query
    
    if search:
        search_filter = f'%{search}%'
        query = query.filter(
            db.or_(
                Product.name.ilike(search_filter),
                Product.sku.ilike(search_filter),
                Product.description.ilike(search_filter)
            )
        )
    
    if category:
        query = query.filter_by(category=category)
    
    products = query.order_by(Product.name).all()
    categories = db.session.query(Product.category).distinct().filter(Product.category.isnot(None)).all()
    categories = sorted([c[0] for c in categories if c[0]])
    
    return render_template('products.html', products=products, categories=categories)

@app.route('/product/add', methods=['GET', 'POST'])
@login_required
def add_product():
    if request.method == 'POST':
        try:
            # Auto-generate SKU if not provided or checkbox is checked
            sku = request.form.get('sku', '').strip()
            auto_generate = request.form.get('auto_generate_sku') == 'on'
            
            if not sku or auto_generate:
                sku = generate_sku()
            
            # Validate required fields
            name = request.form.get('name', '').strip()
            if not name:
                flash('Product name is required', 'danger')
                return redirect(url_for('add_product'))
            
            product = Product(
                sku=sku,
                name=name,
                description=request.form.get('description', '').strip(),
                category=request.form.get('category', '').strip(),
                unit_price=float(request.form.get('unit_price', 0)),
                cost_price=float(request.form.get('cost_price', 0)),
                quantity=int(request.form.get('quantity', 0)),
                reorder_level=int(request.form.get('reorder_level', 10)),
                reorder_quantity=int(request.form.get('reorder_quantity', 50)),
                supplier=request.form.get('supplier', '').strip(),
                location=request.form.get('location', '').strip()
            )
            
            db.session.add(product)
            db.session.commit()
            
            # Create initial stock transaction
            if product.quantity > 0:
                transaction = Transaction(
                    product_id=product.id,
                    transaction_type='in',
                    quantity=product.quantity,
                    unit_price=product.cost_price,
                    total_price=product.quantity * product.cost_price,
                    reference='Initial Stock',
                    notes='Initial inventory',
                    created_by=current_user.username
                )
                db.session.add(transaction)
                db.session.commit()
            
            flash(f'Product added successfully with SKU: {sku}', 'success')
            return redirect(url_for('products'))
        except ValueError as e:
            flash(f'Invalid input: {str(e)}', 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding product: {str(e)}', 'danger')
    
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template('add_product.html', suppliers=suppliers)

@app.route('/product/download-template')
@login_required
def download_template():
    """Generate and download an Excel template for bulk upload"""
    from io import BytesIO
    from flask import send_file
    
    template_data = {
        'sku': ['PROD001', '', 'PROD003'],
        'name': ['Sample Product 1', 'Sample Product 2', 'Sample Product 3'],
        'description': ['Product description here', 'Another description', 'Description text'],
        'category': ['Electronics', 'Furniture', 'Office Supplies'],
        'unit_price': [1500.00, 2500.00, 500.00],
        'cost_price': [1000.00, 1800.00, 300.00],
        'quantity': [100, 50, 200],
        'reorder_level': [10, 5, 20],
        'reorder_quantity': [50, 25, 100],
        'supplier': ['ABC Suppliers', 'XYZ Ltd', 'Office Depot'],
        'location': ['Warehouse A', 'Warehouse B', 'Store Front']
    }
    
    df = pd.DataFrame(template_data)
    output = BytesIO()
    
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Products')
        worksheet = writer.sheets['Products']
        
        for idx, col in enumerate(df.columns):
            max_length = max(df[col].astype(str).apply(len).max(), len(col)) + 2
            worksheet.column_dimensions[chr(65 + idx)].width = max_length
        
        worksheet['A5'] = 'Note: Leave SKU empty for auto-generation'
    
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='product_upload_template.xlsx'
    )

@app.route('/product/bulk-upload', methods=['GET', 'POST'])
@login_required
def bulk_upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file uploaded', 'danger')
            return redirect(request.url)
        
        file = request.files['file']
        
        if file.filename == '':
            flash('No file selected', 'danger')
            return redirect(request.url)
        
        if not allowed_file(file.filename):
            flash('Invalid file type. Please upload an Excel file (.xlsx or .xls)', 'danger')
            return redirect(request.url)
        
        try:
            # Read Excel file directly from memory (no disk write needed!)
            df = pd.read_excel(file, engine='openpyxl')
            
            required_columns = ['name', 'cost_price']
            missing_columns = [col for col in required_columns if col not in df.columns]
            
            if missing_columns:
                flash(f'Missing required columns: {", ".join(missing_columns)}', 'danger')
                return redirect(request.url)
            
            success_count = 0
            error_count = 0
            errors = []
            
            # Process in batches to reduce memory usage
            batch_size = 100
            for batch_start in range(0, len(df), batch_size):
                batch_df = df.iloc[batch_start:batch_start + batch_size]
                
                for index, row in batch_df.iterrows():
                    try:
                        def safe_int(value, default=0):
                            if pd.isna(value) or value == '':
                                return default
                            try:
                                return int(float(value))
                            except (ValueError, TypeError):
                                return default
                        
                        def safe_float(value, default=0.0):
                            if pd.isna(value) or value == '':
                                return default
                            try:
                                return float(value)
                            except (ValueError, TypeError):
                                return default
                        
                        def safe_str(value, default=''):
                            if pd.isna(value) or value == '':
                                return default
                            return str(value).strip()
                        
                        product_name = safe_str(row['name'])
                        
                        if not product_name:
                            raise ValueError("Product name is required")
                        
                        sku = safe_str(row.get('sku', ''))
                        existing_product = Product.query.filter_by(name=product_name).first()
                        
                        if not existing_product and sku:
                            existing_product = Product.query.filter_by(sku=sku).first()
                        
                        if not existing_product and not sku:
                            sku = generate_sku()
                        elif existing_product:
                            sku = existing_product.sku
                        
                        if existing_product:
                            # Update existing
                            existing_product.description = safe_str(row.get('description', ''))
                            existing_product.category = safe_str(row.get('category', ''))
                            existing_product.unit_price = safe_float(row.get('unit_price', existing_product.unit_price))
                            existing_product.cost_price = safe_float(row['cost_price'])
                            existing_product.reorder_level = safe_int(row.get('reorder_level'), 10)
                            existing_product.reorder_quantity = safe_int(row.get('reorder_quantity'), 50)
                            existing_product.supplier = safe_str(row.get('supplier', ''))
                            existing_product.location = safe_str(row.get('location', ''))
                            existing_product.updated_at = datetime.now(timezone.utc)
                            
                            if 'quantity' in row and pd.notna(row['quantity']):
                                new_quantity = safe_int(row['quantity'], 0)
                                quantity_diff = new_quantity - existing_product.quantity
                                existing_product.quantity = new_quantity
                                
                                if quantity_diff != 0:
                                    transaction = Transaction(
                                        product_id=existing_product.id,
                                        transaction_type='in' if quantity_diff > 0 else 'out',
                                        quantity=abs(quantity_diff),
                                        unit_price=existing_product.cost_price,
                                        total_price=abs(quantity_diff) * existing_product.cost_price,
                                        reference='Bulk Upload Update',
                                        notes=f'Quantity adjusted via bulk upload',
                                        created_by=current_user.username
                                    )
                                    db.session.add(transaction)
                        else:
                            # Create new
                            product = Product(
                                sku=sku,
                                name=safe_str(row['name']),
                                description=safe_str(row.get('description', '')),
                                category=safe_str(row.get('category', '')),
                                unit_price=safe_float(row.get('unit_price', 0)),
                                cost_price=safe_float(row['cost_price']),
                                quantity=safe_int(row.get('quantity'), 0),
                                reorder_level=safe_int(row.get('reorder_level'), 10),
                                reorder_quantity=safe_int(row.get('reorder_quantity'), 50),
                                supplier=safe_str(row.get('supplier', '')),
                                location=safe_str(row.get('location', ''))
                            )
                            db.session.add(product)
                            db.session.flush()
                            
                            if product.quantity > 0:
                                transaction = Transaction(
                                    product_id=product.id,
                                    transaction_type='in',
                                    quantity=product.quantity,
                                    unit_price=product.cost_price,
                                    total_price=product.quantity * product.cost_price,
                                    reference='Bulk Upload',
                                    notes='Initial stock from bulk upload',
                                    created_by=current_user.username
                                )
                                db.session.add(transaction)
                        
                        success_count += 1
                        
                    except Exception as e:
                        error_count += 1
                        errors.append(f"Row {index + 2}: {str(e)}")
                
                # Commit each batch
                db.session.commit()
            
            if success_count > 0:
                flash(f'Successfully processed {success_count} products!', 'success')
            if error_count > 0:
                flash(f'{error_count} errors occurred. Check details below.', 'warning')
                for error in errors[:10]:
                    flash(error, 'danger')
            
            return redirect(url_for('products'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error processing file: {str(e)}', 'danger')
            return redirect(request.url)
    
    return render_template('bulk_upload.html')


@app.route('/product/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_product(id):
    product = Product.query.get_or_404(id)
    
    if request.method == 'POST':
        product.sku = request.form['sku']
        product.name = request.form['name']
        product.description = request.form.get('description')
        product.category = request.form.get('category')
        product.unit_price = float(request.form['unit_price'])
        product.cost_price = float(request.form['cost_price'])
        product.reorder_level = int(request.form.get('reorder_level', 10))
        product.reorder_quantity = int(request.form.get('reorder_quantity', 50))
        product.supplier = request.form.get('supplier')
        product.location = request.form.get('location')
        product.updated_at = datetime.now(timezone.utc)
        
        try:
            db.session.commit()
            flash('Product updated successfully!', 'success')
            return redirect(url_for('products'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating product: {str(e)}', 'error')
    
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template('edit_product.html', product=product, suppliers=suppliers)

@app.route('/product/<int:id>/delete', methods=['POST'])
@login_required
def delete_product(id):
    product = Product.query.get_or_404(id)
    
    try:
        db.session.delete(product)
        db.session.commit()
        flash('Product deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting product: {str(e)}', 'error')
    
    return redirect(url_for('products'))

# ... (rest of your routes remain the same, just add created_by=current_user.username to transactions)

@app.route('/stock/in/<int:product_id>', methods=['GET', 'POST'])
@login_required
def stock_in(product_id):
    product = Product.query.get_or_404(product_id)
    
    if request.method == 'POST':
        try:
            quantity = int(request.form['quantity'])
            unit_price = float(request.form.get('unit_price', product.cost_price))
            
            product.quantity += quantity
            product.updated_at = datetime.now(timezone.utc)
            
            transaction = Transaction(
                product_id=product.id,
                transaction_type='in',
                quantity=quantity,
                unit_price=unit_price,
                total_price=quantity * unit_price,
                reference=request.form.get('reference', ''),
                notes=request.form.get('notes', ''),
                created_by=current_user.username
            )
            
            db.session.add(transaction)
            db.session.commit()
            flash(f'Successfully added {quantity} units of {product.name}', 'success')
            return redirect(url_for('products'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating stock: {str(e)}', 'danger')
    
    return render_template('stock_in.html', product=product)

@app.route('/stock/out/<int:product_id>', methods=['GET', 'POST'])
@login_required
def stock_out(product_id):
    product = Product.query.get_or_404(product_id)
    
    if request.method == 'POST':
        try:
            quantity = int(request.form['quantity'])
            
            if quantity > product.quantity:
                flash(f'Insufficient stock! Available: {product.quantity}', 'danger')
                return render_template('stock_out.html', product=product)
            
            unit_price = float(request.form.get('unit_price', product.unit_price))
            reference = request.form.get('reference', '')
            notes = request.form.get('notes', '')
            created_by = request.form.get('created_by', current_user.username)
            
            product.quantity -= quantity
            product.updated_at = datetime.now(timezone.utc)
            
            transaction = Transaction(
                product_id=product.id,
                transaction_type='out',
                quantity=quantity,
                unit_price=unit_price,
                total_price=quantity * unit_price,
                reference=reference,
                notes=notes,
                created_by=created_by
            )
            
            db.session.add(transaction)
            db.session.commit()
            
            # Check for low stock warning
            low_stock_warning = product.quantity <= product.reorder_level
            
            # Prepare receipt data
            receipt_data = {
                'transaction_id': transaction.id,
                'transaction_date': transaction.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'product_name': product.name,
                'product_sku': product.sku,
                'quantity': quantity,
                'unit_price': unit_price,
                'total_price': quantity * unit_price,
                'reference': reference,
                'notes': notes,
                'created_by': created_by,
                'remaining_stock': product.quantity,
                'low_stock_warning': low_stock_warning
            }
            
            # Render template with receipt data
            return render_template('stock_out.html', 
                                 product=product, 
                                 receipt_data=receipt_data,
                                 show_receipt=True)
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating stock: {str(e)}', 'danger')
    
    return render_template('stock_out.html', product=product, show_receipt=False)

@app.route('/transactions')
@login_required
def transactions():
    search = request.args.get('search', '').strip()
    transaction_type = request.args.get('transaction_type', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20

    query = Transaction.query

    if search:
        search_filter = f'%{search}%'
        query = query.join(Transaction.product).filter(
            Product.name.ilike(search_filter)
        )

    if transaction_type:
        query = query.filter(Transaction.transaction_type.ilike(transaction_type))
    
    transactions = query.order_by(Transaction.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return render_template('transactions.html', transactions=transactions, search=search, transaction_type=transaction_type)

@app.route('/api/receipt/<int:transaction_id>')
@login_required
def api_receipt(transaction_id):
    """API endpoint to fetch receipt data for a transaction"""
    transaction = Transaction.query.get_or_404(transaction_id)
    
    # Only allow viewing receipts for 'out' transactions (sales)
    if transaction.transaction_type != 'out':
        return jsonify({'error': 'Receipt only available for sales transactions'}), 400
    
    # Get product details
    product = Product.query.get(transaction.product_id)
    
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    
    # Prepare receipt data (customer-friendly, no internal stock info)
    receipt_data = {
        'transaction_id': transaction.id,
        'transaction_date': transaction.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'product_name': product.name,
        'quantity': transaction.quantity,
        'unit_price': float(transaction.unit_price or 0),
        'total_price': float(transaction.total_price or 0),
        'reference': transaction.reference or '',
        'notes': transaction.notes or '',
        'created_by': transaction.created_by or 'Staff'
    }
    
    return jsonify(receipt_data)

@app.route('/suppliers')
@login_required
def suppliers():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template('suppliers.html', suppliers=suppliers)

@app.route('/supplier/add', methods=['GET', 'POST'])
@login_required
def add_supplier():
    if request.method == 'POST':
        supplier = Supplier(
            name=request.form['name'],
            contact_person=request.form.get('contact_person'),
            email=request.form.get('email'),
            phone=request.form.get('phone'),
            address=request.form.get('address'),
            notes=request.form.get('notes')
        )
        
        try:
            db.session.add(supplier)
            db.session.commit()
            flash('Supplier added successfully!', 'success')
            return redirect(url_for('suppliers'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding supplier: {str(e)}', 'error')
    
    return render_template('add_supplier.html')

@app.route('/sales')
@login_required
def sales():
    search = request.args.get('search', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    sort_by = request.args.get('sort_by', 'total_sales')  # total_sales, quantity, revenue
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    # Base query - get all OUT transactions grouped by product
    query = db.session.query(
        Product.id,
        Product.name,
        Product.sku,
        db.func.sum(Transaction.quantity).label('total_quantity'),
        db.func.sum(Transaction.total_price).label('total_revenue'),
        db.func.count(Transaction.id).label('total_sales'),
        db.func.min(Transaction.created_at).label('first_sale'),
        db.func.max(Transaction.created_at).label('last_sale')
    ).join(
        Transaction, Product.id == Transaction.product_id
    ).filter(
        Transaction.transaction_type == 'out'
    ).group_by(
        Product.id, Product.name, Product.sku
    )
    
    # Search filter
    if search:
        search_filter = f'%{search}%'
        query = query.filter(Product.name.ilike(search_filter))
    
    # Date range filter
    if date_from:
        try:
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(Transaction.created_at >= date_from_obj)
        except ValueError:
            pass
    
    if date_to:
        try:
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            # Add one day to include the entire end date
            date_to_obj = date_to_obj + timedelta(days=1)
            query = query.filter(Transaction.created_at < date_to_obj)
        except ValueError:
            pass
    
    # Sorting
    if sort_by == 'quantity':
        query = query.order_by(db.desc('total_quantity'))
    elif sort_by == 'revenue':
        query = query.order_by(db.desc('total_revenue'))
    else:  # total_sales
        query = query.order_by(db.desc('total_sales'))
    
    # Pagination
    sales_data = query.paginate(page=page, per_page=per_page, error_out=False)
    
    # Calculate totals for summary
    totals = db.session.query(
        db.func.sum(Transaction.quantity).label('total_quantity'),
        db.func.sum(Transaction.total_price).label('total_revenue'),
        db.func.count(Transaction.id).label('total_transactions')
    ).filter(
        Transaction.transaction_type == 'out'
    )
    
    # Apply same filters to totals
    if search:
        totals = totals.join(Product).filter(Product.name.ilike(search_filter))
    if date_from:
        try:
            totals = totals.filter(Transaction.created_at >= datetime.strptime(date_from, '%Y-%m-%d'))
        except ValueError:
            pass
    if date_to:
        try:
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
            totals = totals.filter(Transaction.created_at < date_to_obj)
        except ValueError:
            pass
    
    totals_result = totals.first()
    
    return render_template('sales.html', 
                         sales_data=sales_data, 
                         search=search,
                         date_from=date_from,
                         date_to=date_to,
                         sort_by=sort_by,
                         totals=totals_result)

@app.route('/sales/<int:product_id>')
@login_required
def sales_detail(product_id):
    product = Product.query.get_or_404(product_id)
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    # Get all sales transactions for this product
    transactions = Transaction.query.filter_by(
        product_id=product_id,
        transaction_type='out'
    ).order_by(Transaction.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    # Calculate summary statistics
    stats = db.session.query(
        db.func.sum(Transaction.quantity).label('total_quantity'),
        db.func.sum(Transaction.total_price).label('total_revenue'),
        db.func.count(Transaction.id).label('total_sales'),
        db.func.avg(Transaction.unit_price).label('avg_price'),
        db.func.min(Transaction.created_at).label('first_sale'),
        db.func.max(Transaction.created_at).label('last_sale')
    ).filter_by(
        product_id=product_id,
        transaction_type='out'
    ).first()
    
    return render_template('sales_detail.html', 
                         product=product, 
                         transactions=transactions,
                         stats=stats)

@app.route('/reports')
@login_required
def reports():
    # Get date range
    days = request.args.get('days', 30, type=int)
    start_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    # Stock movement report
    stock_in = db.session.query(
        func.sum(Transaction.quantity).label('total')
    ).filter(
        Transaction.transaction_type == 'in',
        Transaction.created_at >= start_date
    ).scalar() or 0
    
    stock_out = db.session.query(
        func.sum(Transaction.quantity).label('total')
    ).filter(
        Transaction.transaction_type == 'out',
        Transaction.created_at >= start_date
    ).scalar() or 0
    
    # Top selling products
    top_products = db.session.query(
        Product.name,
        func.sum(Transaction.quantity).label('total_sold')
    ).join(
        Transaction
    ).filter(
        Transaction.transaction_type == 'out',
        Transaction.created_at >= start_date
    ).group_by(
        Product.id
    ).order_by(
        func.sum(Transaction.quantity).desc()
    ).limit(10).all()
    
    # Low stock items
    low_stock = Product.query.filter(
        Product.quantity <= Product.reorder_level
    ).all()
    
    # Category distribution
    category_stats = db.session.query(
        Product.category,
        func.count(Product.id).label('count'),
        func.sum(Product.quantity * Product.unit_price).label('value')
    ).group_by(
        Product.category
    ).all()
    
    return render_template('reports.html',
                         stock_in=stock_in,
                         stock_out=stock_out,
                         top_products=top_products,
                         low_stock=low_stock,
                         category_stats=category_stats,
                         days=days)

@app.route('/api/product/<int:id>')
@login_required
def api_product(id):
    product = Product.query.get_or_404(id)
    return jsonify({
        'id': product.id,
        'sku': product.sku,
        'name': product.name,
        'quantity': product.quantity,
        'unit_price': product.unit_price,
        'cost_price': product.cost_price,
        'value': product.quantity * product.unit_price
    })

@app.route('/api/low-stock')
@login_required
def api_low_stock():
    products = Product.query.filter(
        Product.quantity <= Product.reorder_level
    ).all()
    
    return jsonify([{
        'id': p.id,
        'name': p.name,
        'quantity': p.quantity,
        'reorder_level': p.reorder_level
    } for p in products])

# Admin decorator - checks if role is 'admin'
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        if current_user.role != 'admin':
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# User Management Page
@app.route('/users')
@login_required
@admin_required
def users():
    search = request.args.get('search', '').strip()
    role_filter = request.args.get('role', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    query = User.query
    
    if search:
        search_filter = f'%{search}%'
        query = query.filter(
            db.or_(
                User.username.ilike(search_filter),
                User.email.ilike(search_filter)
            )
        )
    
    if role_filter:
        query = query.filter(User.role == role_filter)
    
    users = query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return render_template('users.html', users=users, search=search, role_filter=role_filter)

# Change User Role
@app.route('/users/<int:user_id>/change-role', methods=['POST'])
@login_required
@admin_required
def change_user_role(user_id):
    user = User.query.get_or_404(user_id)
    
    if user.id == current_user.id:
        return jsonify({
            'success': False,
            'message': 'You cannot change your own role'
        }), 400
    
    data = request.get_json()
    new_role = data.get('role')
    
    if new_role not in ['admin', 'user']:
        return jsonify({
            'success': False,
            'message': 'Invalid role'
        }), 400
    
    user.role = new_role
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': f'User {user.username} role changed to {new_role}',
        'role': user.role
    })

# Delete User
@app.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    
    if user.id == current_user.id:
        return jsonify({
            'success': False,
            'message': 'You cannot delete your own account'
        }), 400
    
    username = user.username
    db.session.delete(user)
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': f'User {username} has been deleted successfully'
    })

# Reset User Password
@app.route('/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
@admin_required
def reset_user_password(user_id):
    user = User.query.get_or_404(user_id)
    data = request.get_json()
    new_password = data.get('password')
    
    if not new_password or len(new_password) < 6:
        return jsonify({
            'success': False,
            'message': 'Password must be at least 6 characters long'
        }), 400
    
    user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')  # Use bcrypt instead
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': f'Password for {user.username} has been reset successfully'
    })

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
