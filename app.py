from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from extensions import db, login_manager, bcrypt
from models import User, Product, Transaction, Supplier
import os
import pandas as pd
from werkzeug.utils import secure_filename
import random
import string

# Load environment variables
load_dotenv()

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')

# PostgreSQL Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    # 'postgresql://postgres:password@localhost:5432/pharmacy_db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'xlsx', 'xls'}
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Create upload folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize extensions
db.init_app(app)
login_manager.init_app(app)
bcrypt.init_app(app)

# Configure login manager
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def generate_sku(prefix='PROD'):
    """Generate a unique SKU"""
    while True:
        # Generate random alphanumeric string
        random_part = ''.join(random.choices(string.digits, k=6))
        sku = f"{prefix}{random_part}"
        
        # Check if SKU already exists
        if not Product.query.filter_by(sku=sku).first():
            return sku

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
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
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role')
        
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))
            
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(username=username, email=email, password=hashed_password, role=role)
        db.session.add(user)
        db.session.commit()
        
        flash('Account created successfully!', 'success')
        return redirect(url_for('register'))
    
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
    # Calculate dashboard metrics
    total_products = Product.query.count()
    low_stock_products = Product.query.filter(Product.quantity <= Product.reorder_level).count()
    
    # Calculate total inventory value
    products = Product.query.all()
    total_value = sum(p.quantity * p.unit_price for p in products)
    total_cost = sum(p.quantity * p.cost_price for p in products)
    
    # Get recent transactions
    recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()
    
    # Get low stock alerts
    low_stock_items = Product.query.filter(Product.quantity <= Product.reorder_level).all()
    
    return render_template('dashboard.html',
                         total_products=total_products,
                         low_stock_products=low_stock_products,
                         total_value=total_value,
                         total_cost=total_cost,
                         recent_transactions=recent_transactions,
                         low_stock_items=low_stock_items)

@app.route('/products')
@login_required
def products():
    search = request.args.get('search', '')
    category = request.args.get('category', '')
    
    query = Product.query
    
    if search:
        query = query.filter(
            db.or_(
                Product.name.ilike(f'%{search}%'),
                Product.sku.ilike(f'%{search}%'),
                Product.description.ilike(f'%{search}%')
            )
        )
    
    if category:
        query = query.filter_by(category=category)
    
    products = query.order_by(Product.name).all()
    categories = db.session.query(Product.category).distinct().all()
    categories = [c[0] for c in categories if c[0]]
    
    return render_template('products.html', products=products, categories=categories)

@app.route('/product/add', methods=['GET', 'POST'])
@login_required
def add_product():
    if request.method == 'POST':
        # Auto-generate SKU if not provided or checkbox is checked
        sku = request.form.get('sku', '').strip()
        auto_generate = request.form.get('auto_generate_sku') == 'on'
        
        if not sku or auto_generate:
            sku = generate_sku()
        
        product = Product(
            sku=sku,
            name=request.form['name'],
            description=request.form.get('description'),
            category=request.form.get('category'),
            unit_price=float(request.form['unit_price']),
            cost_price=float(request.form['cost_price']),
            quantity=int(request.form.get('quantity', 0)),
            reorder_level=int(request.form.get('reorder_level', 10)),
            reorder_quantity=int(request.form.get('reorder_quantity', 50)),
            supplier=request.form.get('supplier'),
            location=request.form.get('location')
        )
        
        try:
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
                    notes='Initial inventory'
                )
                db.session.add(transaction)
                db.session.commit()
            
            flash(f'Product added successfully with SKU: {sku}', 'success')
            return redirect(url_for('products'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding product: {str(e)}', 'error')
    
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template('add_product.html', suppliers=suppliers)

@app.route('/product/download-template')
@login_required
def download_template():
    """Generate and download an Excel template for bulk upload"""
    from io import BytesIO
    from flask import send_file
    
    # Create a sample template with mixed SKUs (some provided, some empty for auto-generation)
    template_data = {
        'sku': ['PROD001', '', 'PROD003'],  # Empty SKU will be auto-generated
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
    
    # Create Excel file in memory
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Products')
        
        # Get the worksheet
        worksheet = writer.sheets['Products']
        
        # Adjust column widths
        for idx, col in enumerate(df.columns):
            max_length = max(df[col].astype(str).apply(len).max(), len(col)) + 2
            worksheet.column_dimensions[chr(65 + idx)].width = max_length
        
        # Add a note in the sheet
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
        # Check if file was uploaded
        if 'file' not in request.files:
            flash('No file uploaded', 'danger')
            return redirect(request.url)
        
        file = request.files['file']
        
        if file.filename == '':
            flash('No file selected', 'danger')
            return redirect(request.url)
        
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            try:
                # Read Excel file
                df = pd.read_excel(filepath)
                
                # Required columns
                required_columns = ['name', 'cost_price']
                missing_columns = [col for col in required_columns if col not in df.columns]
                
                if missing_columns:
                    flash(f'Missing required columns: {", ".join(missing_columns)}', 'danger')
                    os.remove(filepath)
                    return redirect(request.url)
                
                success_count = 0
                error_count = 0
                errors = []
                
                for index, row in df.iterrows():
                    try:
                        # Helper function to safely get numeric values
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
                        
                        # Get product name
                        product_name = safe_str(row['name'])
                        
                        if not product_name:
                            raise ValueError("Product name is required")
                        
                        # Get or generate SKU
                        sku = safe_str(row.get('sku', ''))
                        
                        # Check if product exists by name first, then by SKU
                        existing_product = Product.query.filter_by(name=product_name).first()
                        
                        if not existing_product and sku:
                            # If not found by name, check by SKU
                            existing_product = Product.query.filter_by(sku=sku).first()
                        
                        if not existing_product and not sku:
                            # Auto-generate SKU for new products
                            sku = generate_sku()
                        elif existing_product:
                            # Use existing product's SKU if updating
                            sku = existing_product.sku
                        
                        if existing_product:
                            # Update existing product
                            existing_product.name = safe_str(row['name'])
                            existing_product.description = safe_str(row.get('description', ''))
                            existing_product.category = safe_str(row.get('category', ''))
                            existing_product.unit_price = safe_float(row['unit_price'])
                            existing_product.cost_price = safe_float(row['cost_price'])
                            existing_product.reorder_level = safe_int(row.get('reorder_level'), 10)
                            existing_product.reorder_quantity = safe_int(row.get('reorder_quantity'), 50)
                            existing_product.supplier = safe_str(row.get('supplier', ''))
                            existing_product.location = safe_str(row.get('location', ''))
                            existing_product.updated_at = datetime.now(timezone.utc)
                            
                            # Update quantity if provided
                            if 'quantity' in row and pd.notna(row['quantity']):
                                new_quantity = safe_int(row['quantity'], 0)
                                quantity_diff = new_quantity - existing_product.quantity
                                existing_product.quantity = new_quantity
                                
                                # Create transaction for quantity change
                                if quantity_diff != 0:
                                    transaction = Transaction(
                                        product_id=existing_product.id,
                                        transaction_type='in' if quantity_diff > 0 else 'out',
                                        quantity=abs(quantity_diff),
                                        unit_price=existing_product.cost_price,
                                        total_price=abs(quantity_diff) * existing_product.cost_price,
                                        reference='Bulk Upload Update',
                                        notes=f'Quantity adjusted via bulk upload'
                                    )
                                    db.session.add(transaction)
                        else:
                            # Create new product
                            product = Product(
                                sku=sku,
                                name=safe_str(row['name']),
                                description=safe_str(row.get('description', '')),
                                category=safe_str(row.get('category', '')),
                                unit_price=safe_float(row['unit_price']),
                                cost_price=safe_float(row['cost_price']),
                                quantity=safe_int(row.get('quantity'), 0),
                                reorder_level=safe_int(row.get('reorder_level'), 10),
                                reorder_quantity=safe_int(row.get('reorder_quantity'), 50),
                                supplier=safe_str(row.get('supplier', '')),
                                location=safe_str(row.get('location', ''))
                            )
                            db.session.add(product)
                            db.session.flush()  # Get product ID
                            
                            # Create initial stock transaction if quantity > 0
                            if product.quantity > 0:
                                transaction = Transaction(
                                    product_id=product.id,
                                    transaction_type='in',
                                    quantity=product.quantity,
                                    unit_price=product.cost_price,
                                    total_price=product.quantity * product.cost_price,
                                    reference='Bulk Upload',
                                    notes='Initial stock from bulk upload'
                                )
                                db.session.add(transaction)
                        
                        success_count += 1
                        
                    except Exception as e:
                        error_count += 1
                        errors.append(f"Row {index + 2}: {str(e)}")
                
                db.session.commit()
                
                # Clean up uploaded file
                os.remove(filepath)
                
                # Display results
                if success_count > 0:
                    flash(f'Successfully processed {success_count} products!', 'success')
                if error_count > 0:
                    flash(f'{error_count} errors occurred. Check details below.', 'warning')
                    for error in errors[:10]:  # Show first 10 errors
                        flash(error, 'danger')
                
                return redirect(url_for('products'))
                
            except Exception as e:
                db.session.rollback()
                if os.path.exists(filepath):
                    os.remove(filepath)
                flash(f'Error processing file: {str(e)}', 'danger')
                return redirect(request.url)
        else:
            flash('Invalid file type. Please upload an Excel file (.xlsx or .xls)', 'danger')
    
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

@app.route('/stock/in/<int:product_id>', methods=['GET', 'POST'])
@login_required
def stock_in(product_id):
    product = Product.query.get_or_404(product_id)
    
    if request.method == 'POST':
        quantity = int(request.form['quantity'])
        unit_price = float(request.form.get('unit_price', product.cost_price))
        
        # Update product quantity
        product.quantity += quantity
        product.updated_at = datetime.now(timezone.utc)
        
        # Create transaction record
        transaction = Transaction(
            product_id=product.id,
            transaction_type='in',
            quantity=quantity,
            unit_price=unit_price,
            total_price=quantity * unit_price,
            reference=request.form.get('reference'),
            notes=request.form.get('notes'),
            created_by=request.form.get('created_by', 'System')
        )
        
        try:
            db.session.add(transaction)
            db.session.commit()
            flash(f'Successfully added {quantity} units of {product.name}', 'success')
            return redirect(url_for('products'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating stock: {str(e)}', 'error')
    
    return render_template('stock_in.html', product=product)

@app.route('/stock/out/<int:product_id>', methods=['GET', 'POST'])
@login_required
def stock_out(product_id):
    product = Product.query.get_or_404(product_id)
    
    if request.method == 'POST':
        quantity = int(request.form['quantity'])
        
        if quantity > product.quantity:
            flash(f'Insufficient stock! Available: {product.quantity}', 'error')
            return render_template('stock_out.html', product=product)
        
        unit_price = float(request.form.get('unit_price', product.unit_price))
        
        # Update product quantity
        product.quantity -= quantity
        product.updated_at = datetime.now(timezone.utc)
        
        # Get processed by - use form data if provided, otherwise use current user's username
        processed_by = request.form.get('created_by', '').strip()
        if not processed_by:
            processed_by = current_user.username
        
        # Create transaction record
        transaction = Transaction(
            product_id=product.id,
            transaction_type='out',
            quantity=quantity,
            unit_price=unit_price,
            total_price=quantity * unit_price,
            reference=request.form.get('reference'),
            notes=request.form.get('notes'),
            created_by=processed_by
        )
        
        try:
            db.session.add(transaction)
            db.session.commit()
            flash(f'Successfully removed {quantity} units of {product.name}', 'success')
            
            # Check if low stock alert needed
            if product.quantity <= product.reorder_level:
                flash(f'Warning: {product.name} is low on stock ({product.quantity} remaining)', 'warning')
            
            return redirect(url_for('products'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating stock: {str(e)}', 'error')
    
    return render_template('stock_out.html', product=product)

@app.route('/transactions')
@login_required
def transactions():
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    transactions = Transaction.query.order_by(Transaction.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return render_template('transactions.html', transactions=transactions)

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

# Initialize database
# def init_database():
#     with app.app_context():
#         db.create_all()

#         if not User.query.filter_by(username='admin').first():
#             hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
#             admin = User(
#                 username='admin',
#                 email='admin@example.com',
#                 password=hashed_password,
#                 role='admin'
#             )
#             db.session.add(admin)
#             db.session.commit()

    # init_database()
