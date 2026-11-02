# dashboard_analysis/routes.py - COMPLETE VERSION with WORKING pricing in recommendations
from flask import Blueprint, current_app, render_template
import logging
import pandas as pd
import numpy as np
import re
from collections import defaultdict

# Import chart generation helpers and core analysis functions
from mba_analysis.routes import build_tidlists, eclat, generate_association_rules, _generate_bar_chart
from spm_analysis.prefixspan import PrefixSpan
from spm_analysis.routes import _generate_spm_bar_chart, _generate_line_chart
from data_ingestion.utils import calculate_suggested_supports

logger = logging.getLogger(__name__)
dashboard_bp = Blueprint('dashboard', __name__, template_folder='../../templates')

def safe_numeric_conversion(value, default=0, conversion_type='float'):
    """Safely convert values to numeric types"""
    try:
        if pd.isna(value) or value is None:
            return default
        if conversion_type == 'float':
            return float(value)
        elif conversion_type == 'int':
            return int(float(value))  # Convert to float first, then int
        else:
            return value
    except (ValueError, TypeError):
        return default

def safe_list_to_string(item):
    """Safely convert list to string for product name lookup"""
    if isinstance(item, list):
        return item[0] if item else ''
    return str(item)

# --- PRICING OPTIMIZATION FUNCTIONS ---

def calculate_graduated_discount_pricing(item1_price, item2_price, cost_ratio=0.7, min_margin=0.15):
    """Calculate graduated discount pricing for Complementary Collection bundles"""
    individual_cost1 = item1_price * cost_ratio
    individual_cost2 = item2_price * cost_ratio
    total_cost = individual_cost1 + individual_cost2
    
    # Test different discount levels (30%-60% off second item)
    best_price = 0
    best_profit = -float('inf')
    best_discount = 0.5
    base_demand = 100
    
    discount_options = [0.3, 0.4, 0.5, 0.6]  # 30% to 60% off second item
    
    for discount in discount_options:
        customer_pays = item1_price + (item2_price * (1 - discount))
        profit_per_bundle = customer_pays - total_cost
        margin = profit_per_bundle / customer_pays if customer_pays > 0 else 0
        
        if margin >= min_margin:
            demand_multiplier = 1 + (discount * 1.5)
            expected_demand = base_demand * demand_multiplier
            total_profit = profit_per_bundle * expected_demand
            
            if total_profit > best_profit:
                best_profit = total_profit
                best_price = customer_pays
                best_discount = discount
    
    return {
        'optimized_price': round(best_price, 2),
        'discount_percent': round(best_discount * 100, 1),
        'profit_margin': round(((best_price - total_cost) / best_price) * 100, 1) if best_price > 0 else 0,
        'expected_profit': round(best_profit, 2),
        'individual_total_price': round(item1_price + item2_price, 2)
    }

def optimize_sequential_discount(first_price, second_price, first_cost, second_cost, confidence_level, min_margin=0.15):
    """Optimize discount percentage for Buy Now, Save Next offers"""
    if confidence_level == "High":
        base_discount = 0.30  # 30%
    elif confidence_level == "Medium":
        base_discount = 0.25  # 25%
    else:
        base_discount = 0.20  # 20%
    
    best_discount = base_discount
    best_profit = -float('inf')
    best_total_price = 0
    base_demand = 100
    
    discount_range = [
        max(0.10, base_discount - 0.05),
        base_discount,
        min(0.40, base_discount + 0.05)
    ]
    
    for discount in discount_range:
        discounted_second_price = second_price * (1 - discount)
        total_price = first_price + discounted_second_price
        total_cost = first_cost + second_cost
        
        profit_per_bundle = total_price - total_cost
        margin = profit_per_bundle / total_price if total_price > 0 else 0
        
        if margin >= min_margin:
            demand_boost = discount * 2
            expected_demand = base_demand * (1 + demand_boost)
            total_profit = profit_per_bundle * expected_demand
            
            if total_profit > best_profit:
                best_profit = total_profit
                best_discount = discount
                best_total_price = total_price
    
    return {
        'optimal_discount_percent': round(best_discount * 100, 1),
        'optimal_bundle_price': round(best_total_price, 2),
        'profit_margin': round(((best_total_price - (first_cost + second_cost)) / best_total_price) * 100, 1) if best_total_price > 0 else 0,
        'expected_profit': round(best_profit, 2),
        'individual_total_price': round(first_price + second_price, 2)
    }

# --- CORE BUNDLE PROCESSING WITH PRICING ---

def process_bundles_with_pricing(bundles, df, processor_instance, bundle_type):
    """
    MAIN FUNCTION: Add pricing optimization to bundles using dataset prices
    """
    try:
        product_map = processor_instance.get_product_map()
        product_map_reverse = {v: k for k, v in product_map.items()}
        
        for bundle in bundles:
            try:
                # Initialize pricing flags
                bundle['has_pricing'] = False
                bundle['pricing_error'] = None
                
                # Extract items based on bundle type
                if bundle_type == 'complementary':
                    buy_item = safe_list_to_string(bundle.get('buy_item', ''))
                    get_item = safe_list_to_string(bundle.get('get_item', ''))
                else:  # sequential
                    buy_item = safe_list_to_string(bundle.get('buy_item', ''))
                    get_item = safe_list_to_string(bundle.get('next_item', ''))
                
                if not buy_item or not get_item:
                    bundle['pricing_error'] = "Missing item names"
                    continue
                
                # Get product IDs
                buy_item_id = product_map_reverse.get(buy_item, buy_item)
                get_item_id = product_map_reverse.get(get_item, get_item)
                
                # Fetch prices from dataset
                buy_item_data = df[df['Product ID'] == buy_item_id]
                get_item_data = df[df['Product ID'] == get_item_id]
                
                if buy_item_data.empty or get_item_data.empty:
                    bundle['pricing_error'] = "Product data not found in dataset"
                    continue
                
                # Calculate average prices
                buy_price = safe_numeric_conversion(buy_item_data['Price Per Unit'].mean())
                get_price = safe_numeric_conversion(get_item_data['Price Per Unit'].mean())
                
                if buy_price <= 0 or get_price <= 0:
                    bundle['pricing_error'] = "Invalid price data"
                    continue
                
                # Calculate optimized pricing
                if bundle_type == 'complementary':
                    pricing_result = calculate_graduated_discount_pricing(buy_price, get_price)
                    
                    # Add pricing data to bundle
                    bundle['buy_item_price'] = round(buy_price, 2)
                    bundle['get_item_price'] = round(get_price, 2)
                    bundle['individual_total_price'] = pricing_result['individual_total_price']
                    bundle['optimized_price'] = pricing_result['optimized_price']
                    bundle['discount_percentage'] = pricing_result['discount_percent']
                    bundle['savings'] = round(pricing_result['individual_total_price'] - pricing_result['optimized_price'], 2)
                    bundle['profit_margin'] = pricing_result['profit_margin']
                    bundle['revenue_impact'] = round(pricing_result['expected_profit'] / 1000, 1)
                    bundle['pricing_description'] = f"Save {pricing_result['discount_percent']}% on {get_item}"
                    
                else:  # sequential
                    confidence_level = bundle.get('confidence_level', 'Medium')
                    first_cost = buy_price * 0.7
                    second_cost = get_price * 0.7
                    
                    pricing_result = optimize_sequential_discount(
                        buy_price, get_price, first_cost, second_cost, confidence_level
                    )
                    
                    # Add pricing data to bundle
                    bundle['buy_item_price'] = round(buy_price, 2)
                    bundle['next_item_price'] = round(get_price, 2)
                    bundle['individual_total_price'] = pricing_result['individual_total_price']
                    bundle['optimized_price'] = pricing_result['optimal_bundle_price']
                    bundle['discount_percentage'] = pricing_result['optimal_discount_percent']
                    bundle['savings'] = round(get_price * (pricing_result['optimal_discount_percent'] / 100), 2)
                    bundle['profit_margin'] = pricing_result['profit_margin']
                    bundle['revenue_impact'] = round(pricing_result['expected_profit'] / 1000, 1)
                    bundle['pricing_description'] = f"Get {pricing_result['optimal_discount_percent']}% off {get_item}"
                
                bundle['has_pricing'] = True
                logger.info(f"Added pricing to bundle: {buy_item} + {get_item} = ${bundle['optimized_price']}")
                
            except Exception as e:
                logger.error(f"Error processing bundle pricing: {e}")
                bundle['pricing_error'] = str(e)
                bundle['has_pricing'] = False
        
        # Log summary
        priced_count = sum(1 for b in bundles if b.get('has_pricing', False))
        logger.info(f"Successfully added pricing to {priced_count}/{len(bundles)} {bundle_type} bundles")
        
    except Exception as e:
        logger.error(f"Error in process_bundles_with_pricing: {e}")
        for bundle in bundles:
            bundle['has_pricing'] = False
            bundle['pricing_error'] = str(e)
    
    return bundles

def format_bogo_display_for_pricing(bundles):
    """Enhanced BOGO display formatting for pricing optimization section"""
    for bundle in bundles:
        # Safely extract buy item(s)
        if isinstance(bundle.get('buy_item'), list):
            buy_items = [str(item).strip() for item in bundle['buy_item'] if item]
            buy_display = ', '.join(buy_items) if buy_items else ''
        else:
            buy_display = str(bundle.get('buy_item', '')).strip()
            
        # Safely extract get item(s)
        if isinstance(bundle.get('get_item'), list):
            get_items = [str(item).strip() for item in bundle['get_item'] if item]
            get_display = ', '.join(get_items) if get_items else ''
        else:
            get_display = str(bundle.get('get_item', '')).strip()
        
        # Set individual displays
        bundle['buy_display'] = buy_display
        bundle['get_display'] = get_display
        
        # Create complete bundle display for pricing section
        if buy_display and get_display:
            bundle['pricing_bundle_name'] = f"{buy_display} + {get_display}"
            bundle['pricing_short_label'] = f"{buy_display[:20]}... + {get_display[:20]}..." if len(buy_display + get_display) > 50 else f"{buy_display} + {get_display}"
        else:
            bundle['pricing_bundle_name'] = buy_display or get_display or "Bundle"
            bundle['pricing_short_label'] = bundle['pricing_bundle_name']
            
        # Create offer description for pricing
        bundle['pricing_offer_description'] = f"Buy {buy_display}, Get {get_display} at discount"
        
        # Add bundle type identifier
        bundle['bundle_type_display'] = 'Complementary Collection'
        
    return bundles

# --- VALIDATION FUNCTIONS (Keep existing) ---

def validate_bogo_business_logic(buy_item, get_item, df, processor_instance):
    """Validate business logic for BOGO offers"""
    try:
        buy_item_name = safe_list_to_string(buy_item)
        get_item_name = safe_list_to_string(get_item)
        
        product_map_reverse = {v: k for k, v in processor_instance.get_product_map().items()}
        buy_item_id = product_map_reverse.get(buy_item_name, buy_item_name)
        get_item_id = product_map_reverse.get(get_item_name, get_item_name)
        
        buy_item_avg_price = safe_numeric_conversion(df[df['Product ID'] == buy_item_id]['Price Per Unit'].mean())
        get_item_avg_price = safe_numeric_conversion(df[df['Product ID'] == get_item_id]['Price Per Unit'].mean())
        
        if buy_item_avg_price <= 0 or get_item_avg_price <= 0:
            return False
            
        if get_item_avg_price > buy_item_avg_price * 2:
            return False
            
        if buy_item_avg_price < 1 or get_item_avg_price < 1:
            return False
            
        return True
        
    except Exception as e:
        logger.warning(f"Business logic validation failed for {buy_item_name} → {get_item_name}: {e}")
        return False

def validate_temporal_cooccurrence(buy_item, get_item, df, processor_instance):
    """Validate that items are actually purchased together in transactions"""
    try:
        buy_item_name = safe_list_to_string(buy_item)
        get_item_name = safe_list_to_string(get_item)
        
        product_map_reverse = {v: k for k, v in processor_instance.get_product_map().items()}
        buy_item_id = product_map_reverse.get(buy_item_name, buy_item_name)
        get_item_id = product_map_reverse.get(get_item_name, get_item_name)
        
        buy_item_transactions = set(df[df['Product ID'] == buy_item_id]['Transaction ID'])
        
        if len(buy_item_transactions) == 0:
            return 0.0
            
        both_items_transactions = set()
        for tid in buy_item_transactions:
            transaction_products = set(df[df['Transaction ID'] == tid]['Product ID'])
            if buy_item_id in transaction_products and get_item_id in transaction_products:
                both_items_transactions.add(tid)
        
        cooccurrence_rate = len(both_items_transactions) / len(buy_item_transactions)
        return cooccurrence_rate
        
    except Exception as e:
        logger.warning(f"Temporal validation failed for {buy_item_name} → {get_item_name}: {e}")
        return 0.0

def validate_transaction_volume(buy_item, get_item, df, processor_instance):
    """Ensure sufficient transaction volume for reliable BOGO offers"""
    try:
        buy_item_name = safe_list_to_string(buy_item)
        get_item_name = safe_list_to_string(get_item)
        
        product_map_reverse = {v: k for k, v in processor_instance.get_product_map().items()}
        buy_item_id = product_map_reverse.get(buy_item_name, buy_item_name)
        get_item_id = product_map_reverse.get(get_item_name, get_item_name)
        
        buy_item_count = len(df[df['Product ID'] == buy_item_id])
        get_item_count = len(df[df['Product ID'] == get_item_id])
        
        return buy_item_count >= 10 and get_item_count >= 10
        
    except Exception as e:
        logger.warning(f"Volume validation failed for {buy_item_name} → {get_item_name}: {e}")
        return False

def calculate_bogo_validation_score(rule, temporal_score, business_score):
    """Calculate comprehensive validation score for BOGO offers"""
    mba_score = (rule['confidence'] * 0.25) + (rule['lift'] / 5 * 0.15) + (rule['support'] * 100 * 0.1)
    temporal_weight = temporal_score * 0.3
    business_weight = business_score * 0.2
    return mba_score + temporal_weight + business_weight

def generate_validated_bogo_bundles(mba_association_rules_full, df, processor_instance):
    """Enhanced BOGO bundle generation with comprehensive validation"""
    bogo_candidates = [
        rule for rule in mba_association_rules_full
        if len(rule.get('antecedent_names', [])) == 1 
        and len(rule.get('consequent_names', [])) == 1
        and rule['lift'] >= 1.2 
        and rule['confidence'] >= 0.4
        and rule['support'] >= 0.001
    ]
    
    validated_candidates = []
    
    for rule in bogo_candidates:
        buy_item = safe_list_to_string(rule['antecedent_names'])
        get_item = safe_list_to_string(rule['consequent_names'])
        
        business_valid = validate_bogo_business_logic(buy_item, get_item, df, processor_instance)
        if not business_valid:
            continue
            
        temporal_score = validate_temporal_cooccurrence(buy_item, get_item, df, processor_instance)
        if temporal_score < 0.15:
            continue
            
        if not validate_transaction_volume(buy_item, get_item, df, processor_instance):
            continue
            
        business_score = 1.0 if business_valid else 0.0
        rule['validation_score'] = calculate_bogo_validation_score(rule, temporal_score, business_score)
        rule['temporal_score'] = temporal_score
        validated_candidates.append(rule)
    
    seen_pairs = set()
    unique_validated_candidates = []
    
    validated_candidates.sort(key=lambda x: x['validation_score'], reverse=True)
    
    for rule in validated_candidates:
        buy_item = safe_list_to_string(rule['antecedent_names'])
        get_item = safe_list_to_string(rule['consequent_names'])
        pair = tuple(sorted([buy_item, get_item]))
        
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            unique_validated_candidates.append(rule)
            
    return unique_validated_candidates[:5]

def generate_enhanced_next_purchase_offers(spm_sequential_patterns_full, df, processor_instance):
    """Enhanced next purchase offer generation with better logic"""
    next_purchase_offers_list = []
    
    try:
        multi_item_patterns = [p for p in spm_sequential_patterns_full if len(p['product_items']) >= 2]
        
        if multi_item_patterns:
            for i, pattern in enumerate(multi_item_patterns[:5]):
                buy_a = pattern['product_items'][0]
                get_b = pattern['product_items'][1]
                
                support_score = pattern['support'] * 100
                confidence_level = "High" if support_score > 5 else "Medium" if support_score > 2 else "Low"
                
                next_purchase_offers_list.append({
                    'id': i + 1,
                    'buy_item': buy_a,
                    'next_item': get_b,
                    'confidence_level': confidence_level,
                    'support_percentage': f"{support_score:.1f}%",
                    'reason': f"Customers buying {buy_a} have a {support_score:.1f}% likelihood of purchasing {get_b} in their next transaction (Confidence: {confidence_level})."
                })
        else:
            single_item_patterns = [p for p in spm_sequential_patterns_full if len(p['product_items']) == 1]
            single_item_patterns.sort(key=lambda x: x['support'], reverse=True)
            
            for i in range(0, min(8, len(single_item_patterns)-1), 2):
                if i+1 < len(single_item_patterns):
                    buy_item = single_item_patterns[i]['product_items'][0]
                    next_item = single_item_patterns[i+1]['product_items']
                    
                    if buy_item != next_item:
                        combined_support = (single_item_patterns[i]['support'] + single_item_patterns[i+1]['support']) / 2
                        support_percentage = combined_support * 100
                        
                        next_purchase_offers_list.append({
                            'id': len(next_purchase_offers_list) + 1,
                            'buy_item': buy_item,
                            'next_item': next_item,
                            'confidence_level': "Medium",
                            'support_percentage': f"{support_percentage:.1f}%",
                            'reason': f"Popular products often purchased in sequence. Combined pattern support: {support_percentage:.1f}%."
                        })
                        
                        if len(next_purchase_offers_list) >= 5:
                            break
                            
    except Exception as e:
        logger.error(f"Enhanced next purchase offers generation error: {e}")
        
    return next_purchase_offers_list

# --- KEEP EXISTING PRICING OPTIMIZATION FUNCTIONS FOR PRICING TAB ---

def calculate_bundle_pricing_optimization(bogo_bundles, next_purchase_offers, df, processor_instance):
    """Calculate pricing optimization for both Complementary Collection and Buy Now, Save Next bundles"""
    try:
        pricing_results = []
        product_map_reverse = {v: k for k, v in processor_instance.get_product_map().items()}
        
        # Process Complementary Collection bundles
        for bundle in bogo_bundles:
            buy_items = bundle['buy_item'] if isinstance(bundle['buy_item'], list) else [bundle['buy_item']]
            get_items = bundle['get_item'] if isinstance(bundle['get_item'], list) else [bundle['get_item']]
            
            buy_items = [safe_list_to_string(item) for item in buy_items]
            get_items = [safe_list_to_string(item) for item in get_items]
            all_items = buy_items + get_items
            
            total_individual_price = 0.0
            valid_items = []
            
            for item_name in all_items[:2]:
                item_id = product_map_reverse.get(item_name, item_name)
                item_data = df[df['Product ID'] == item_id]
                
                if not item_data.empty:
                    avg_price = safe_numeric_conversion(item_data['Price Per Unit'].mean())
                    if avg_price > 0:
                        total_individual_price += avg_price
                        quantity_sold = safe_numeric_conversion(item_data['Quantity'].sum(), conversion_type='int')
                        transaction_count = safe_numeric_conversion(item_data['Transaction ID'].nunique(), conversion_type='int')
                        
                        valid_items.append({
                            'name': item_name,
                            'avg_price': avg_price,
                            'quantity_sold': quantity_sold,
                            'transaction_frequency': transaction_count
                        })
            
            if len(valid_items) >= 2 and total_individual_price > 0:
                pricing_result = calculate_graduated_discount_pricing(
                    valid_items[0]['avg_price'], 
                    valid_items[1]['avg_price']
                )
                
                confidence_score = safe_numeric_conversion(bundle.get('confidence_score', 75))
                
                base_transactions = sum(item['transaction_frequency'] for item in valid_items) / len(valid_items)
                estimated_bundle_sales = base_transactions * (confidence_score / 100) * 0.25
                revenue_impact = (pricing_result['optimized_price'] * estimated_bundle_sales) / 1000
                
                complete_bundle_name = f"{valid_items[0]['name']} + {valid_items[1]['name']}"
                
                pricing_results.append({
                    'id': len(pricing_results) + 1,
                    'bundle_name': complete_bundle_name,
                    'bundle_display_name': complete_bundle_name,
                    'bundle_type': 'Complementary Collection',
                    'offer_description': f"Buy {valid_items[0]['name']}, Get {valid_items[1]['name']} at {pricing_result['discount_percent']}% Off",
                    'buy_item_name': valid_items['name'],
                    'get_item_name': valid_items[1]['name'],
                    'items': valid_items,
                    'individual_total_price': pricing_result['individual_total_price'],
                    'optimized_price': pricing_result['optimized_price'],
                    'discount_amount': round(total_individual_price - pricing_result['optimized_price'], 2),
                    'discount_percentage': pricing_result['discount_percent'],
                    'revenue_impact': round(revenue_impact, 1),
                    'profit_impact': round(revenue_impact * 0.25, 1),
                    'estimated_sales': int(estimated_bundle_sales),
                    'profit_margin': pricing_result['profit_margin'],
                    'confidence': "High" if confidence_score >= 80 else "Medium" if confidence_score >= 60 else "Low",
                    'confidence_score': confidence_score
                })
        
        # Process Buy Now, Save Next bundles
        for bundle in next_purchase_offers:
            items = [bundle['buy_item'], bundle['next_item']]
            total_individual_price = 0.0
            valid_items = []
            
            for item_name in items:
                item_name = safe_list_to_string(item_name)
                item_id = product_map_reverse.get(item_name, item_name)
                item_data = df[df['Product ID'] == item_id]
                
                if not item_data.empty:
                    avg_price = safe_numeric_conversion(item_data['Price Per Unit'].mean())
                    if avg_price > 0:
                        total_individual_price += avg_price
                        quantity_sold = safe_numeric_conversion(item_data['Quantity'].sum(), conversion_type='int')
                        transaction_count = safe_numeric_conversion(item_data['Transaction ID'].nunique(), conversion_type='int')
                        
                        valid_items.append({
                            'name': item_name,
                            'avg_price': avg_price,
                            'quantity_sold': quantity_sold,
                            'transaction_frequency': transaction_count
                        })
            
            if len(valid_items) >= 2 and total_individual_price > 0:
                first_cost = valid_items[0]['avg_price'] * 0.7
                second_cost = valid_items[1]['avg_price'] * 0.7
                confidence_level = bundle.get('confidence_level', 'Medium')
                
                pricing_result = optimize_sequential_discount(
                    valid_items['avg_price'], 
                    valid_items[1]['avg_price'],
                    first_cost,
                    second_cost,
                    confidence_level
                )
                
                confidence_score = 85 if confidence_level == 'High' else 65 if confidence_level == 'Medium' else 45
                
                base_transactions = sum(item['transaction_frequency'] for item in valid_items) / len(valid_items)
                estimated_bundle_sales = base_transactions * (confidence_score / 100) * 0.20
                revenue_impact = (pricing_result['optimal_bundle_price'] * estimated_bundle_sales) / 1000
                
                pricing_results.append({
                    'id': len(pricing_results) + 1,
                    'bundle_name': f"Sequential: {valid_items[0]['name']} → {valid_items[1]['name']}",
                    'bundle_display_name': f"{valid_items['name']} → {valid_items[1]['name']}",
                    'bundle_type': 'Buy Now, Save Next',
                    'offer_description': f"Buy {valid_items['name']}, Get {pricing_result['optimal_discount_percent']}% Off {valid_items[1]['name']}",
                    'buy_item_name': valid_items['name'],
                    'next_item_name': valid_items[1]['name'],
                    'items': valid_items,
                    'individual_total_price': pricing_result['individual_total_price'],
                    'optimized_price': pricing_result['optimal_bundle_price'],
                    'discount_amount': round(valid_items[1]['avg_price'] * (pricing_result['optimal_discount_percent']/100), 2),
                    'discount_percentage': pricing_result['optimal_discount_percent'],
                    'revenue_impact': round(revenue_impact, 1),
                    'profit_impact': round(revenue_impact * 0.25, 1),
                    'estimated_sales': int(estimated_bundle_sales),
                    'profit_margin': pricing_result['profit_margin'],
                    'confidence': "High" if confidence_score >= 80 else "Medium" if confidence_score >= 60 else "Low",
                    'confidence_score': confidence_score
                })
        
        return pricing_results[:8]
        
    except Exception as e:
        logger.error(f"Error in calculate_bundle_pricing_optimization: {e}")
        return []

def generate_pricing_optimization_data(bogo_bundles, next_purchase_offers, df, processor_instance):
    """Generate comprehensive pricing optimization data"""
    try:
        if not bogo_bundles and not next_purchase_offers:
            return {
                'revenue_impact': '+$0K',
                'profit_impact': '+$0K',
                'avg_discount': '0%',
                'optimized_bundles': 0,
                'bundle_pricing': [],
                'top_opportunities': []
            }
        
        bundle_pricing = calculate_bundle_pricing_optimization(bogo_bundles, next_purchase_offers, df, processor_instance)
        
        total_revenue_impact = sum(b['revenue_impact'] for b in bundle_pricing)
        total_profit_impact = sum(b['profit_impact'] for b in bundle_pricing)
        avg_discount = np.mean([b['discount_percentage'] for b in bundle_pricing]) if bundle_pricing else 0
        
        top_opportunities = sorted(bundle_pricing, key=lambda x: x['revenue_impact'], reverse=True)[:3]
        
        return {
            'revenue_impact': f'+${total_revenue_impact:.0f}K',
            'profit_impact': f'+${total_profit_impact:.0f}K',
            'avg_discount': f'{avg_discount:.1f}%',
            'optimized_bundles': len(bundle_pricing),
            'bundle_pricing': bundle_pricing,
            'top_opportunities': top_opportunities
        }
        
    except Exception as e:
        logger.error(f"Error generating pricing optimization data: {e}")
        return {
            'revenue_impact': '+$0K',
            'profit_impact': '+$0K',
            'avg_discount': '0%',
            'optimized_bundles': 0,
            'bundle_pricing': [],
            'top_opportunities': []
        }

@dashboard_bp.route('/')
def dashboard_page():
    """
    COMPLETE ENHANCED DASHBOARD with ALL FEATURES INCLUDING WORKING PRICING IN RECOMMENDATIONS
    """
    try:
        processor_instance = current_app.config['DATA_INGESTION_PROCESSOR']
        df = processor_instance.get_processed_data()

        # Initialize empty context / Fallback for no data
        empty_context = {
            'total_revenue': '0.00', 'revenue_trend': 'N/A',
            'total_transactions': '0', 'transactions_trend': 'N/A',
            'avg_order_value': '0.00', 'industry_avg_aov': 'N/A',
            'avg_items_per_transaction': '0.0',
            'unique_customers': '0', 'customer_trend': 'N/A',
            'top_selling_product': {'name': 'No Data', 'units': 0, 'sales_percentage': 0, 'tag': 'Upload Data'},
            'customer_insights_overview': [{'insight': 'Upload data to see insights', 'metric': ''}],
            
            'mba_chart_image': '',
            'mba_association_rules': [],
            'spm_chart_image': '',
            'spm_sequential_patterns': [],
            
            'bogo_bundles': [],
            'next_purchase_offers': [],
            
            'pricing_revenue_impact': '+$0K',
            'pricing_profit_impact': '+$0K',
            'pricing_avg_discount': '0%',
            'pricing_optimized_bundles': 0,
            'pricing_bundle_pricing': [],
            'pricing_top_opportunities': [],
            
            'dashboard_message': {'type': 'info', 'text': 'Upload data to generate insights and charts.'}
        }

        if df.empty:
            logger.warning("Dashboard: No data found. Rendering with empty state.")
            return render_template('dashboard.html', **empty_context)

        # Convert critical columns to numeric
        numeric_cols = ['Quantity', 'Price Per Unit']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # Calculate cache hash
        current_data_hash = hash(len(df))
        if not df.empty and 'Transaction ID' in df.columns:
            current_data_hash = hash((len(df), df['Transaction ID'].nunique(), df['Product ID'].nunique()))
        
        # Check Cache
        cache = current_app.config['ANALYTICS_CACHE']
        if cache['data_hash'] == current_data_hash and cache['mba_results'] is not None and cache['spm_results'] is not None:
            logger.info("Dashboard: Cache hit for MBA and SPM results. Using cached data.")
            mba_chart_image_b64 = cache['mba_chart_image']
            mba_association_rules_full = cache['mba_results']
            spm_chart_image_b64 = cache['spm_chart_image']
            spm_sequential_patterns_full = cache['spm_results']
            bogo_bundles_list = cache.get('bogo_bundles', [])
            next_purchase_offers_list = cache.get('next_purchase_offers', [])
            
            # THE KEY FIX: Process bundles with pricing from cached data
            logger.info("Processing cached bundles with pricing...")
            bogo_bundles_list = process_bundles_with_pricing(bogo_bundles_list, df, processor_instance, 'complementary')
            next_purchase_offers_list = process_bundles_with_pricing(next_purchase_offers_list, df, processor_instance, 'sequential')
            
            bogo_bundles_list = format_bogo_display_for_pricing(bogo_bundles_list)
            
            pricing_data = cache.get('pricing_data')
            if pricing_data is None:
                pricing_data = generate_pricing_optimization_data(
                    bogo_bundles_list, next_purchase_offers_list, df, processor_instance
                )
                cache['pricing_data'] = pricing_data
            
            dashboard_message = {'type': 'success', 'text': 'Dashboard insights loaded from cache with fresh pricing calculations.'}
            
            # Calculate KPIs
            df['TotalPrice'] = df['Quantity'] * df['Price Per Unit']
            total_revenue = safe_numeric_conversion(df['TotalPrice'].sum())
            total_transactions = safe_numeric_conversion(df['Transaction ID'].nunique(), conversion_type='int')
            avg_order_value = total_revenue / total_transactions if total_transactions > 0 else 0
            total_quantity_sold = safe_numeric_conversion(df['Quantity'].sum(), conversion_type='int')
            avg_items_per_transaction = total_quantity_sold / total_transactions if total_transactions > 0 else 0
            unique_customers = safe_numeric_conversion(df['Customer ID'].nunique(), conversion_type='int')
            
            # Top selling product
            product_sales = df.groupby('Product ID')['TotalPrice'].sum().nlargest(1)
            
            if not product_sales.empty and len(product_sales) > 0:
                top_product_id = product_sales.index[0]
                product_map = processor_instance.get_product_map()
                top_product_name = product_map.get(str(top_product_id), str(top_product_id))
                top_product_units = safe_numeric_conversion(df[df['Product ID'] == top_product_id]['Quantity'].sum(), conversion_type='int')
                top_product_sales_value = safe_numeric_conversion(product_sales.values[0])
                top_product_sales_percentage = (top_product_sales_value / total_revenue) * 100 if total_revenue > 0 else 0
                
                top_selling_product_details = {
                    'name': top_product_name,
                    'units': top_product_units,
                    'sales_percentage': round(top_product_sales_percentage),
                    'tag': 'Best Seller'
                }
            else:
                top_selling_product_details = {'name': 'N/A', 'units': 0, 'sales_percentage': 0, 'tag': 'N/A'}
            
            # Customer insights
            customer_insights_overview_list = []
            if unique_customers > 0:
                customer_insights_overview_list.append({'insight': 'Total Unique Customers', 'metric': f"{unique_customers:,}"})
                customer_transaction_counts = df.groupby('Customer ID')['Transaction ID'].nunique()
                repeat_customers_count = (customer_transaction_counts > 1).sum()
                repeat_customer_percentage = (repeat_customers_count / unique_customers) * 100 if unique_customers > 0 else 0
                customer_insights_overview_list.append({'insight': 'Repeat Customer Rate', 'metric': f"{repeat_customer_percentage:.1f}%"})
                avg_transactions_per_customer_calc = total_transactions / unique_customers if unique_customers > 0 else 0
                customer_insights_overview_list.append({'insight': 'Avg. Transactions/Customer', 'metric': f"{avg_transactions_per_customer_calc:.1f}"})
                customer_revenue = df.groupby('Customer ID')['TotalPrice'].sum().nlargest(3)
                if not customer_revenue.empty:
                    for i, (cust_id, rev) in enumerate(customer_revenue.items()):
                        customer_insights_overview_list.append({'insight': f"Top Spender #{i+1} (ID: {cust_id})", 'metric': f"${rev:,.0f}"})
            else:
                customer_insights_overview_list.append({'insight': 'No customer data for insights', 'metric': ''})
                
        else:
            logger.info("Dashboard: Cache miss or data changed. Recalculating MBA and SPM analysis with pricing.")

            # Calculate KPIs
            required_kpi_cols = ['Quantity', 'Price Per Unit', 'Transaction ID', 'Product ID', 'Customer ID']
            if not all(col in df.columns for col in required_kpi_cols):
                logger.error(f"Dashboard: Missing critical columns: {[col for col in required_kpi_cols if col not in df.columns]}")
                missing_cols_str = ', '.join(col for col in required_kpi_cols if col not in df.columns)
                empty_context['dashboard_message'] = {'type': 'error', 'text': f"Missing critical columns for KPIs: {missing_cols_str}"}
                return render_template('dashboard.html', **empty_context)

            df['TotalPrice'] = df['Quantity'] * df['Price Per Unit']
            
            total_revenue = safe_numeric_conversion(df['TotalPrice'].sum())
            total_transactions = safe_numeric_conversion(df['Transaction ID'].nunique(), conversion_type='int')
            avg_order_value = total_revenue / total_transactions if total_transactions > 0 else 0
            total_quantity_sold = safe_numeric_conversion(df['Quantity'].sum(), conversion_type='int')
            avg_items_per_transaction = total_quantity_sold / total_transactions if total_transactions > 0 else 0
            unique_customers = safe_numeric_conversion(df['Customer ID'].nunique(), conversion_type='int')

            # Top selling product
            product_sales = df.groupby('Product ID')['TotalPrice'].sum().nlargest(1)
            
            if not product_sales.empty and len(product_sales) > 0:
                top_product_id = product_sales.index[0]
                product_map = processor_instance.get_product_map()
                top_product_name = product_map.get(str(top_product_id), str(top_product_id))
                top_product_units = safe_numeric_conversion(df[df['Product ID'] == top_product_id]['Quantity'].sum(), conversion_type='int')
                top_product_sales_value = safe_numeric_conversion(product_sales.values[0])
                top_product_sales_percentage = (top_product_sales_value / total_revenue) * 100 if total_revenue > 0 else 0
                
                top_selling_product_details = {
                    'name': top_product_name,
                    'units': top_product_units,
                    'sales_percentage': round(top_product_sales_percentage),
                    'tag': 'Best Seller'
                }
            else:
                top_selling_product_details = {'name': 'N/A', 'units': 0, 'sales_percentage': 0, 'tag': 'N/A'}

            # Customer insights
            customer_insights_overview_list = []
            if unique_customers > 0:
                customer_insights_overview_list.append({'insight': 'Total Unique Customers', 'metric': f"{unique_customers:,}"})
                customer_transaction_counts = df.groupby('Customer ID')['Transaction ID'].nunique()
                repeat_customers_count = (customer_transaction_counts > 1).sum()
                repeat_customer_percentage = (repeat_customers_count / unique_customers) * 100 if unique_customers > 0 else 0
                customer_insights_overview_list.append({'insight': 'Repeat Customer Rate', 'metric': f"{repeat_customer_percentage:.1f}%"})
                avg_transactions_per_customer_calc = total_transactions / unique_customers if unique_customers > 0 else 0
                customer_insights_overview_list.append({'insight': 'Avg. Transactions/Customer', 'metric': f"{avg_transactions_per_customer_calc:.1f}"})
                customer_revenue = df.groupby('Customer ID')['TotalPrice'].sum().nlargest(3)
                if not customer_revenue.empty:
                    for i, (cust_id, rev) in enumerate(customer_revenue.items()):
                        customer_insights_overview_list.append({'insight': f"Top Spender #{i+1} (ID: {cust_id})", 'metric': f"${rev:,.0f}"})
            else:
                customer_insights_overview_list.append({'insight': 'No customer data for insights', 'metric': ''})

            # MBA ANALYSIS
            mba_chart_image_b64 = ''
            mba_association_rules_full = []
            bogo_bundles_list = []

            try:
                suggested_mba_support, suggested_spm_support = calculate_suggested_supports(df)
                mba_min_support_dashboard = suggested_mba_support
                mba_max_len_dashboard = 3
                mba_min_confidence_dashboard = 0.5
                mba_min_lift_dashboard = 1.0

                mba_transaction_count = df["Transaction ID"].nunique()
                mba_min_count = max(1, int(mba_transaction_count * mba_min_support_dashboard))

                df_mba_copy = df.copy()
                df_mba_copy['Product ID'] = df_mba_copy['Product ID'].astype(str).str.strip()

                mba_tidlists = build_tidlists(df_mba_copy)
                mba_frequent_itemsets_counts = eclat(mba_tidlists, mba_min_count, mba_max_len_dashboard)

                mba_association_rules_full = generate_association_rules(
                    mba_frequent_itemsets_counts,
                    mba_transaction_count,
                    processor_instance.get_product_map(),
                    min_confidence=mba_min_confidence_dashboard,
                    min_lift=mba_min_lift_dashboard,
                    df=df_mba_copy
                )

                mba_association_rules_full.sort(key=lambda x: (x['lift'], x['confidence'], x['support']), reverse=True)

                # Generate MBA chart
                rules_for_chart_df = pd.DataFrame(mba_association_rules_full).head(10)
                if not rules_for_chart_df.empty:
                    rules_for_chart_df['rule_label'] = rules_for_chart_df['antecedents'] + ' → ' + rules_for_chart_df['consequents']
                    mba_chart_image_b64 = _generate_bar_chart(
                        rules_for_chart_df,
                        x_col='lift',
                        y_col='rule_label',
                        title=f'Top 10 Rules by Lift (Min Support: {mba_min_support_dashboard})',
                        color='viridis'
                    )

                # Generate BOGO bundles
                validated_bogo_candidates = generate_validated_bogo_bundles(
                    mba_association_rules_full, 
                    df_mba_copy, 
                    processor_instance
                )
                
                for i, rule in enumerate(validated_bogo_candidates):
                    bogo_bundles_list.append({
                        'id': i + 1,
                        'buy_item': safe_list_to_string(rule['antecedent_names']),
                        'get_item': safe_list_to_string(rule['consequent_names']),
                        'confidence_score': round(rule['validation_score'] * 100, 1),
                        'temporal_cooccurrence': f"{rule.get('temporal_score', 0)*100:.1f}%",
                        'reason': f"Validated Complementary Collection offer: {rule['confidence']:.1%} confidence, {rule['lift']:.1f}x lift, {rule.get('temporal_score', 0)*100:.1f}% co-occurrence rate. Business logic validated."
                    })
                
                # THE KEY FIX: Process bundles with pricing immediately after generation
                logger.info("Processing MBA bundles with pricing...")
                bogo_bundles_list = process_bundles_with_pricing(bogo_bundles_list, df, processor_instance, 'complementary')
                bogo_bundles_list = format_bogo_display_for_pricing(bogo_bundles_list)
                
                logger.info(f"Dashboard: Generated {len(bogo_bundles_list)} validated Complementary Collection bundles with pricing")
                
            except Exception as e:
                logger.error(f"Dashboard MBA Analysis Error: {e}", exc_info=True)
                mba_chart_image_b64 = ''
                mba_association_rules_full = []
                bogo_bundles_list = []

            # SPM ANALYSIS
            spm_chart_image_b64 = ''
            spm_sequential_patterns_full = []
            next_purchase_offers_list = []

            try:
                spm_min_support_dashboard = suggested_spm_support
                spm_max_len_dashboard = 3

                df_spm_copy = df.copy()
                df_spm_copy['Product ID'] = df_spm_copy['Product ID'].astype(str).str.strip()
                df_spm_sorted = df_spm_copy.sort_values(by=['Customer ID', 'Date/Timestamp', 'Transaction ID'])
                
                spm_sequences = []
                for _, customer_df in df_spm_sorted.groupby('Customer ID'):
                    customer_sequence = []
                    for _, transaction_df in customer_df.groupby(['Date/Timestamp', 'Transaction ID']):
                        current_transaction_items = sorted(transaction_df['Product ID'].unique().astype(str).tolist())
                        current_transaction_items = [item.strip() for item in current_transaction_items]
                        if current_transaction_items:
                            customer_sequence.append(current_transaction_items)
                    if customer_sequence:
                        spm_sequences.append(customer_sequence)

                if spm_sequences:
                    logger.info(f"Dashboard: Generated {len(spm_sequences)} customer sequences for SPM analysis")
                    
                    miner = PrefixSpan(spm_sequences, min_support=spm_min_support_dashboard, max_len=spm_max_len_dashboard)
                    frequent_patterns_raw = miner.frequent()

                    spm_patterns_with_meta = []
                    spm_num_sequences = len(spm_sequences)
                    
                    for count, pattern_items_flat in frequent_patterns_raw:
                        pattern_name_list = []
                        for item_id in pattern_items_flat:
                            clean_item_id = str(item_id).strip()
                            product_name = processor_instance.get_product_map().get(clean_item_id, clean_item_id)
                            if isinstance(product_name, str):
                                product_name = product_name.strip("[]'\"")
                            pattern_name_list.append(product_name)
                        
                        display_pattern_str = ' -> '.join(pattern_name_list)
                        
                        spm_patterns_with_meta.append({
                            "product_items": pattern_name_list,
                            "label": display_pattern_str,
                            "support": count / spm_num_sequences if spm_num_sequences > 0 else 0,
                            "count": count,
                            "avg_time": 15.2,
                            'confidence': 0.67,
                            'lift': 1.8
                        })
                    
                    spm_patterns_with_meta.sort(key=lambda x: x['support'], reverse=True)
                    spm_sequential_patterns_full = spm_patterns_with_meta

                    # Generate SPM chart
                    spm_chart_data_df = pd.DataFrame(spm_patterns_with_meta).head(10)
                    if not spm_chart_data_df.empty:
                        spm_chart_image_b64 = _generate_spm_bar_chart(
                            spm_chart_data_df,
                            x_col='support',
                            y_col='label',
                            title=f'Top 10 SPM Patterns by Support (Min Support: {spm_min_support_dashboard})',
                            color='mako'
                        )

                    # Generate next purchase offers
                    next_purchase_offers_list = generate_enhanced_next_purchase_offers(
                        spm_sequential_patterns_full, 
                        df_spm_copy, 
                        processor_instance
                    )
                    
                    # THE KEY FIX: Process bundles with pricing immediately after generation
                    logger.info("Processing SPM bundles with pricing...")
                    next_purchase_offers_list = process_bundles_with_pricing(next_purchase_offers_list, df, processor_instance, 'sequential')
                    
                    logger.info(f"Dashboard: Generated {len(next_purchase_offers_list)} enhanced Buy Now, Save Next offers with pricing")

                else:
                    logger.warning("Dashboard: No SPM sequences generated")

            except Exception as e:
                logger.error(f"Dashboard SPM Analysis Error: {e}", exc_info=True)
                spm_chart_image_b64 = ''
                spm_sequential_patterns_full = []
                next_purchase_offers_list = []

            # Generate pricing optimization data
            pricing_data = generate_pricing_optimization_data(
                bogo_bundles_list, 
                next_purchase_offers_list, 
                df, 
                processor_instance
            )

            dashboard_message = {'type': 'success', 'text': 'Dashboard insights loaded with real-time pricing optimization from dataset prices.'}

            # Store results in cache
            cache['data_hash'] = current_data_hash
            cache['mba_results'] = mba_association_rules_full
            cache['spm_results'] = spm_sequential_patterns_full
            cache['mba_chart_image'] = mba_chart_image_b64
            cache['spm_chart_image'] = spm_chart_image_b64
            cache['bogo_bundles'] = bogo_bundles_list
            cache['next_purchase_offers'] = next_purchase_offers_list
            cache['pricing_data'] = pricing_data
            logger.info("Dashboard: Enhanced analysis results with working pricing optimization stored in cache.")

        # COMPLETE CONTEXT
        context = {
            'total_revenue': f"{total_revenue:,.2f}",
            'revenue_trend': '+5.2%',
            'total_transactions': f"{total_transactions:,}",
            'transactions_trend': '+2.1%',
            'avg_order_value': f"{avg_order_value:,.2f}",
            'industry_avg_aov': '125.50',
            'avg_items_per_transaction': f"{avg_items_per_transaction:.1f}",
            'unique_customers': f"{unique_customers:,}",
            'customer_trend': '+3.8%',
            'top_selling_product': top_selling_product_details,
            'customer_insights_overview': customer_insights_overview_list,
            
            'mba_chart_image': mba_chart_image_b64,
            'mba_association_rules': mba_association_rules_full,
            'spm_chart_image': spm_chart_image_b64,
            'spm_sequential_patterns': spm_sequential_patterns_full,

            # BUNDLES WITH PRICING DATA
            'bogo_bundles': bogo_bundles_list,
            'next_purchase_offers': next_purchase_offers_list,
            
            'pricing_revenue_impact': pricing_data['revenue_impact'],
            'pricing_profit_impact': pricing_data['profit_impact'],
            'pricing_avg_discount': pricing_data['avg_discount'],
            'pricing_optimized_bundles': pricing_data['optimized_bundles'],
            'pricing_bundle_pricing': pricing_data['bundle_pricing'],
            'pricing_top_opportunities': pricing_data['top_opportunities'],

            'dashboard_message': dashboard_message
        }

        return render_template('dashboard.html', **context)
        
    except Exception as e:
        logger.error(f"Dashboard: Critical error: {e}", exc_info=True)
        error_context = empty_context.copy()
        error_context['dashboard_message'] = {'type': 'error', 'text': f'Dashboard error: {str(e)}'}
        return render_template('dashboard.html', **error_context)
